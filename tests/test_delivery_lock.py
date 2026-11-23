from concurrent.futures import ThreadPoolExecutor
from threading import Event
import sqlite3

import db
import repo
import worker


def test_parallel_senders_deliver_one_notification(database):
    entered, release = Event(), Event()
    sent = []
    now = db.now().replace(hour=10)
    repo.enqueue_notification("lock-test", "ou_sender", "{}", scheduled_at=now)

    def sender(row):
        sent.append(row.uid)
        entered.set()
        assert release.wait(5)
        return True

    with ThreadPoolExecutor() as executor:
        first = executor.submit(worker.drain_outbox, now, sender)
        try:
            assert entered.wait(5)
            assert worker.drain_outbox(now, sender)["sent"] == 0
        finally:
            release.set()
        assert first.result()["sent"] == 1
    assert worker.drain_outbox(now, sender)["sent"] == 0
    assert len(sent) == 1


def test_other_process_database_lock_blocks_delivery(database):
    now = db.now().replace(hour=10)
    repo.enqueue_notification("external-lock", "ou_sender", "{}", scheduled_at=now)
    with sqlite3.connect(database + ".delivery-lock.db") as connection:
        connection.execute("BEGIN IMMEDIATE")
        assert worker.drain_outbox(now, lambda row: True)["sent"] == 0
    assert worker.drain_outbox(now, lambda row: True)["sent"] == 1
