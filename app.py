import asyncio
import logging
import os
import sys

import autologging
import backoff
import quart
from quart import abort, jsonify, request, Quart
from quart.logging import _setup_logging_queue as setup_logging_queue

from awl import AWL, AWLConnectionError, AWLLoginError
from timed_cache import timed_cache

default_logging_formatter = logging.Formatter(
    "%(asctime)s:%(levelname)s:%(name)s:%(funcName)s:%(message)s",
)

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.ERROR)
console_handler.setFormatter(default_logging_formatter)
syslog_handler = logging.handlers.SysLogHandler(facility='local1')
syslog_handler.setLevel(logging.INFO)
syslog_handler.setFormatter(default_logging_formatter)

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(setup_logging_queue(console_handler))
root_logger.addHandler(setup_logging_queue(syslog_handler))

# Set default levels
logging.getLogger('awl.AWL').setLevel(logging.ERROR)
logging.getLogger('quart.app').setLevel(logging.INFO)
# Suppress access logs by default
logging.getLogger('quart.serving').setLevel(logging.ERROR)


# Monkeypatch Quart's logging functions so
# they don't force their own handlers too far down
# the logging hierarchy
quart.app.create_logger = (
    lambda app: logging.getLogger('quart.app')
)
quart.app.create_serving_logger = (
    lambda: logging.getLogger('quart.serving')
)


app = Quart(__name__)
# Different defaults based on development vs production
if app.env in ('development', 'testing',):
    app.config.from_mapping(
        WEBSOCKETS_WARN_AFTER_DISCONNECTED=0,
    )
elif app.env == 'production':
    app.config.from_mapping(
        WEBSOCKETS_WARN_AFTER_DISCONNECTED=10,
    )

# environment common defaults
app.config.from_mapping(
    LOG_DIRECTORY=app.instance_path,
    TRACE_LOG=None,
    ACCESS_LOG='access.log',
)

# Load configuration file, if present
app.config.from_envvar('WATERFURNACE_CONFIG', silent=True)

# Validate configuration
required_config_keys = [
    'WATERFURNACE_USER',
    'WATERFURNACE_PASSWORD',
    'LOG_DIRECTORY',
]
for name in required_config_keys:
    if name not in app.config:
        print(f"{name} is a required configuration variable")
        sys.exit(255)

if app.config.get('ACCESS_LOG') is not None:
    access_handler = logging.handlers.TimedRotatingFileHandler(
        os.path.join(app.config['LOG_DIRECTORY'], app.config['ACCESS_LOG']),
        when='midnight'
    )
    access_handler.setFormatter(
        logging.Formatter('%(asctime)s %(message)s')
    )
    access_handler.setLevel(logging.INFO)
    access_logger = logging.getLogger('quart.serving')
    access_logger.setLevel(logging.INFO)
    # Disable propagation so access lines don't show
    # up in any other logs
    access_logger.propagate = False
    access_logger.addHandler(setup_logging_queue(access_handler))

if app.config.get('TRACE_LOG') is not None:
    trace_handler = logging.handlers.TimedRotatingFileHandler(
        os.path.join(app.config['LOG_DIRECTORY'], app.config['TRACE_LOG']),
        when='midnight'
    )
    trace_handler.setLevel(autologging.TRACE)
    trace_handler.setFormatter(logging.Formatter(
        "%(asctime)s:%(process)s:%(levelname)s:%(filename)s:"
        "%(lineno)s:%(name)s:%(funcName)s:%(message)s"
    ))
    logging.getLogger().addHandler(setup_logging_queue(trace_handler))
    logging.getLogger().setLevel(autologging.TRACE)

    logging.getLogger("awl.AWL").setLevel(autologging.TRACE)
    logging.getLogger("websockets").setLevel(logging.DEBUG)
    logging.getLogger("quart").setLevel(logging.DEBUG)


async def awl_reconnection_handler():
    try:
        await app.awl_connection.wait_closed()
        app.logger.debug('app.awl_connection.wait_closed() finished')
    except AWLConnectionError:
        try:
            app.logger.info('Logging out of AWL')
            app.awl_connection.logout()
            app.logger.info('AWL logout complete')
        except AWLLoginError:
            app.logger.info('AWL logout failed; ignoring')
            pass

    # Re-establish session whenever wait_closed returns,
    # whether with an exception or not
    await asyncio.sleep(1)
    app.logger.info('Reconnecting to AWL')
    await establish_awl_session()


async def backoff_handler(details):
    try:
        max_elapsed = float(app.config['websockets_warn_after_disconnected'])
    except ValueError:
        max_elapsed = 0.0

    if details['elapsed'] > max_elapsed:
        app.logger.critical("Cannot reconnect to AWL after {tries} tries "
                            "over {elapsed:0.1f} seconds. "
                            "Retrying in {wait:0.1f} "
                            "seconds.".format(**details))


async def backoff_success_handler(details):
    if details['tries'] > 1:
        app.logger.warning("Reconnected to AWL after {elapsed:0.1f} "
                           "seconds ({tries} tries)".format(**details))


@app.before_serving
@backoff.on_exception(backoff.expo,
                      AWLConnectionError,
                      on_backoff=backoff_handler,
                      on_success=backoff_success_handler)
async def establish_awl_session():
    app.awl_connection = AWL(
        app.config['WATERFURNACE_USER'],
        app.config['WATERFURNACE_PASSWORD']
    )
    await app.awl_connection.connect()
    asyncio.create_task(awl_reconnection_handler())


@app.after_serving
async def close_awl_session():
    await app.awl_connection.close()


# Cache reads for 10 seconds to keep from hammering
# the Symphony API
@timed_cache(seconds=10)
async def awl_read_gateway(gwid):
    return await app.awl_connection.read(gwid)


def awl_enumerate_gateways():
    awl_login_data = app.awl_connection.login_data
    gateways = list()
    for location in awl_login_data['locations']:
        for gateway in location['gateways']:
            try:
                gateways.append({
                    'location': location.get('description'),
                    'gwid': gateway['gwid'],
                    'system_name': gateway.get('description'),
                })
            except KeyError:
                app.logger.error("Couldn't get gwid")

    return gateways


def awl_enumerate_zones():
    awl_login_data = app.awl_connection.login_data
    thermostats = list()
    for location in awl_login_data['locations']:
        for gateway in location['gateways']:
            for key, zone_name in gateway['tstat_names'].items():
                if zone_name is not None:
                    try:
                        thermostats.append({
                            'location': location.get('description'),
                            'gwid': gateway['gwid'],
                            'system_name': gateway.get('description'),
                            'zoneid': int(key[1:]),
                            'zone_name': zone_name,
                        })
                    except ValueError:
                        app.logger.error(
                            "Couldn't convert zone key \"{key[1:]}\" to int"
                        )
                    except KeyError:
                        app.logger.error("Couldn't get gwid")

    return thermostats


@app.route('/zones')
async def list_thermostats():
    return jsonify(awl_enumerate_zones())


@app.route('/gateways')
async def list_gateways():
    if 'raw' in request.args:
        return jsonify(app.awl_connection.login_data)
    return jsonify(awl_enumerate_gateways())


@app.route('/gateways/<gwid>')
async def read_gateway(gwid):
    gateway_data = await awl_read_gateway(gwid)
    return jsonify(gateway_data)


@app.route('/gateways/<gwid>/zones')
async def list_gateway_zones(gwid):
    if gwid == '*':
        return await list_thermostats()

    gateway_zones = [
        zone for
        zone in awl_enumerate_zones()
        if zone['gwid'] == gwid
    ]
    return jsonify(gateway_zones)


@app.route('/gateways/<gwid>/zones/<int:zoneid>')
async def view_gateway_zone(gwid, zoneid):
    gateway_zone = [
        zone for
        zone in awl_enumerate_zones()
        if zone['gwid'] == gwid and zone['zoneid'] == zoneid
    ]
    if len(gateway_zone) == 0:
        abort(404)
    if len(gateway_zone) > 1:
        abort(500)
    return jsonify(gateway_zone[0])


@app.route('/gateways/<gwid>/zones/<int:zoneid>/details')
async def read_zone(gwid, zoneid):
    gateway_data = await awl_read_gateway(gwid)

    # Find all zone-specific data
    # in the gateway
    zone_prefix = f"iz2_z{zoneid}_"
    zone_raw_data = {
        key: value for
        (key, value) in gateway_data.items()
        if key.startswith(zone_prefix)
    }

    # Pull e.g. $.iz2_z1_activesettings.* up
    # to the top level
    zone_data = dict()
    zone_data.update(
        zone_raw_data.pop(f"{zone_prefix}activesettings", dict())
    )
    zone_data.update(zone_raw_data)

    # Strip the prefix
    response_data = {
        key.replace(zone_prefix, '', 1): value
        for (key, value)
        in zone_data.items()
    }

    return jsonify(response_data)
