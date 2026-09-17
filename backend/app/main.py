"""Application entry point: HTTP + WebSocket + MQTT in one asyncio loop."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from . import repository
from .api.routes import router
from .config import Settings, get_settings
from .db import Database
from .mqtt_service import MqttService
from .train_service import TrainService
from .ws_hub import Broadcaster


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )


logger = logging.getLogger("app.main")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = Database(settings.database_url)
        await database.create_all()
        broadcaster = Broadcaster()

        mqtt = MqttService(settings, handler=lambda topic, payload: service.handle_message(topic, payload))
        service = TrainService(database, mqtt, broadcaster, settings)
        mqtt.on_connect = service.publish_initial_state

        app.state.settings = settings
        app.state.db = database
        app.state.service = service
        app.state.mqtt = mqtt
        app.state.broadcaster = broadcaster

        await mqtt.start()
        await service.start_background_tasks()
        logger.info("LEGO train control backend started")
        try:
            yield
        finally:
            await service.stop_background_tasks()
            await mqtt.stop()
            await database.dispose()
            logger.info("Backend stopped")

    app = FastAPI(
        title="LEGO Train Control",
        description="Control a fleet of LEGO trains driven by Raspberry Pi Pico W devices.",
        version="1.0.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(router, prefix=settings.api_prefix)

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        """Push live train state to the browser.

        The client receives a full snapshot on connect and incremental
        updates afterwards, so it never has to poll.
        """
        await websocket.accept()
        service: TrainService = websocket.app.state.service
        broadcaster: Broadcaster = websocket.app.state.broadcaster
        await broadcaster.register(websocket)
        try:
            async with service.db.session() as session:
                trains = await repository.list_trains(session)
                from .train_service import serialize_train

                snapshot = [serialize_train(train) for train in trains]
            await websocket.send_json(
                {
                    "type": "snapshot",
                    "data": {"trains": snapshot, "stats": await service.stats()},
                }
            )
            while True:
                # Inbound text is only used as a keepalive ping from the UI.
                message = await websocket.receive_text()
                if message == "ping":
                    await websocket.send_json({"type": "pong", "data": None})
        except WebSocketDisconnect:
            pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("WebSocket closed: %s", exc)
        finally:
            await broadcaster.unregister(websocket)

    return app


app = create_app()
