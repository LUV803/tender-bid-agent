"""Celery application configuration.

The broker and result backend are Redis.  Full job state (progress and stage
results) lives in ``bidding.job_state``; the Celery result backend only stores
the tiny return value of each task.

On Windows run the worker with ``--pool=solo``.  Celery's prefork pool has no
usable semaphore support there, and parallel document processing should be
scaled with multiple worker processes on a POSIX host instead.
"""

from __future__ import annotations

from celery import Celery

from .config import get_settings

settings = get_settings()

celery_app = Celery(
    "bidding",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["bidding.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    enable_utc=True,
    task_default_queue="bidding",
    task_default_exchange="bidding",
    task_default_routing_key="bidding",
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    result_expires=settings.celery_result_expires,
    task_time_limit=settings.celery_task_time_limit,
    task_soft_time_limit=settings.celery_soft_time_limit,
    broker_connection_retry_on_startup=True,
    # Do not redeliver a long document while the worker is still parsing it.
    broker_transport_options={
        "visibility_timeout": max(settings.celery_task_time_limit + 3600, 43200)
    },
)
