#!/usr/bin/env python3
import asyncio
import logging
import sys
from time import sleep
from datetime import datetime, timedelta

from aiohttp import ClientConnectionError, ServerDisconnectedError
from apscheduler.schedulers import SchedulerAlreadyRunningError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from nio import (
    AsyncClient,
    AsyncClientConfig,
    InviteMemberEvent,
    LocalProtocolError,
    LoginError,
    MegolmEvent,
    RoomMessageText,
    UnknownEvent,
)

from matrix_misp_bot.callbacks import Callbacks
from matrix_misp_bot.config import Config
from matrix_misp_bot.storage import Storage
from matrix_misp_bot.mispalert import MISPAlert

logger = logging.getLogger(__name__)


async def main():
    """The first function that is run when starting the bot"""

    # Read user-configured options from a config file.
    # A different config file path can be specified as the first command line argument
    if len(sys.argv) > 1:
        config_path = sys.argv[1]
    else:
        config_path = "config.yaml"

    # Read the parsed config file and create a Config object
    config = Config(config_path)

    # Configure the database
    store = Storage(config.database)

    # Configuration options for the AsyncClient
    client_config = AsyncClientConfig(
        max_limit_exceeded=0,
        max_timeouts=0,
        store_sync_tokens=True,
        encryption_enabled=True,
    )

    # Initialize the matrix client
    client = AsyncClient(
        config.homeserver_url,
        config.user_id,
        device_id=config.device_id,
        store_path=config.store_path,
        config=client_config,
    )

    if config.user_token:
        client.access_token = config.user_token
        client.user_id = config.user_id

    # Set up event callbacks
    callbacks = Callbacks(client, store, config)
    client.add_event_callback(callbacks.message, (RoomMessageText,))
    client.add_event_callback(callbacks.invite, (InviteMemberEvent,))
    client.add_event_callback(callbacks.decryption_failure, (MegolmEvent,))
    client.add_event_callback(callbacks.unknown, (UnknownEvent,))

    # Set up a scheduler
    scheduler = AsyncIOScheduler()

    # Set up MISPAlert
    misp_alert = MISPAlert(client, config, store)

    # Add a job that checks for new taged events every minute
    trigger = IntervalTrigger(
        seconds=60, start_date=datetime.now() + timedelta(seconds=2),
    )

    # Add the query job
    scheduler.add_job(misp_alert.alerter, trigger=trigger)

    # Keep trying to reconnect on failure (with some time in-between)
    while True:
        try:
            if config.user_token:
                # Use token to log in
                client.load_store()

                # Sync encryption keys with the server
                if client.should_upload_keys:
                    await client.keys_upload()
            else:
                # Try to login with the configured username/password
                try:
                    login_response = await client.login(
                        password=config.user_password,
                        device_name=config.device_name,
                    )

                    # Check if login failed
                    if type(login_response) == LoginError:
                        logger.error("Failed to login: %s", login_response.message)
                        return False
                except LocalProtocolError as e:
                    # There's an edge case here where the user hasn't installed the correct C
                    # dependencies. In that case, a LocalProtocolError is raised on login.
                    logger.fatal(
                        "Failed to login. Have you installed the correct dependencies? "
                        "https://github.com/poljar/matrix-nio#installation "
                        "Error: %s",
                        e,
                    )
                    return False

                # Login succeeded!

            logger.info(f"Logged in as {config.user_id}")

            # Allow jobs to fire
            try:
                scheduler.start()
            except SchedulerAlreadyRunningError:
                pass

            await client.sync_forever(timeout=30000, full_state=True)

        except (ClientConnectionError, ServerDisconnectedError):
            logger.warning("Unable to connect to homeserver, retrying in 15s...")

            # Sleep so we don't bombard the server with login requests
            sleep(15)
        finally:
            # Make sure to close the client connection on disconnect
            await client.close()


# Run the main function in an asyncio event loop
asyncio.get_event_loop().run_until_complete(main())
