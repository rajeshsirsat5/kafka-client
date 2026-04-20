"""
kafka_dump.py
─────────────────────────────────────────────────────────────────────────────
Pulls JSON messages from a production Kafka topic for a given date range
and dumps them to a .jsonl file (one JSON object per line).

Usage:
    python kafka_dump.py \
        --topic prod.data.raw \
        --start "2025-04-17 00:00:00" \
        --end   "2025-04-17 23:59:59" \
        --out   messages_fri_17apr.jsonl

Requirements:
    pip install confluent-kafka boto3 pydantic-settings
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from confluent_kafka import Consumer, TopicPartition, KafkaError

# ── Reuse your existing SSL config helper ────────────────────────────────────
# Assumes get_kafka_ssl_config() returns a dict with keys:
#   bootstrap.servers, security.protocol, ssl.ca.location,
#   ssl.certificate.location, ssl.key.location  (or ssl.keystore.* for .p12)
from your_project.kafka_config import get_kafka_ssl_config  # adjust import path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("kafka_dump")


# ─────────────────────────────────────────────────────────────────────────────
# 1. Argument parsing
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Dump Kafka messages to a .jsonl file")
    p.add_argument("--topic",  required=True,  help="Kafka topic name")
    p.add_argument("--start",  required=True,  help="Start datetime  e.g. '2025-04-17 00:00:00'")
    p.add_argument("--end",    required=True,  help="End datetime    e.g. '2025-04-17 23:59:59'")
    p.add_argument("--out",    default="kafka_dump.jsonl", help="Output file path")
    p.add_argument("--tz",     default="UTC",  help="Timezone for --start/--end (default: UTC)")
    p.add_argument("--group",  default="kafka-dump-local-debug",
                   help="Consumer group id (auto-reset, won't affect prod offsets)")
    return p.parse_args()


# ─────────────────────────────────────────────────────────────────────────────
# 2. Timestamp helpers
# ─────────────────────────────────────────────────────────────────────────────

def to_epoch_ms(dt_str: str, tz_name: str) -> int:
    """Parse a datetime string and return epoch milliseconds in UTC."""
    import zoneinfo
    tz = zoneinfo.ZoneInfo(tz_name)
    dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
    return int(dt.timestamp() * 1000)


def from_epoch_ms(epoch_ms: int) -> str:
    """Human-readable UTC string from epoch ms."""
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3. Offset resolution
# ─────────────────────────────────────────────────────────────────────────────

def resolve_offsets(
    consumer: Consumer,
    topic: str,
    start_ms: int,
    end_ms: int,
) -> list[dict]:
    """
    Returns a list of partition ranges:
        [{ partition, start_offset, end_offset }, ...]

    Partitions with no data in the range are skipped.
    """
    metadata = consumer.list_topics(topic, timeout=10)
    if topic not in metadata.topics:
        raise RuntimeError(f"Topic '{topic}' not found on broker.")

    partitions = list(metadata.topics[topic].partitions.keys())
    log.info("Topic '%s' has %d partition(s): %s", topic, len(partitions), partitions)

    # Build TopicPartition lists for start and end timestamps
    start_tps = [TopicPartition(topic, p, start_ms) for p in partitions]
    end_tps   = [TopicPartition(topic, p, end_ms)   for p in partitions]

    start_offsets = consumer.offsets_for_times(start_tps, timeout=10)
    end_offsets   = consumer.offsets_for_times(end_tps,   timeout=10)

    ranges = []
    for p in partitions:
        s = next(tp for tp in start_offsets if tp.partition == p)
        e = next(tp for tp in end_offsets   if tp.partition == p)

        if s.offset < 0:
            # -1 means no message at or after start_ms on this partition
            log.warning("Partition %d: no messages found for the given range — skipping.", p)
            continue

        end_offset = e.offset if e.offset >= 0 else None  # None = consume to end of log

        log.info(
            "Partition %d: offsets [%d → %s]  (%s → %s)",
            p,
            s.offset,
            end_offset if end_offset is not None else "end-of-log",
            from_epoch_ms(start_ms),
            from_epoch_ms(end_ms),
        )
        ranges.append({"partition": p, "start_offset": s.offset, "end_offset": end_offset})

    return ranges


# ─────────────────────────────────────────────────────────────────────────────
# 4. Consume & dump
# ─────────────────────────────────────────────────────────────────────────────

def consume_and_dump(
    consumer: Consumer,
    topic: str,
    partition_ranges: list[dict],
    start_ms: int,
    end_ms: int,
    out_path: Path,
) -> int:
    """
    Consumes messages across all partition ranges and writes them to a .jsonl file.
    Returns total message count written.
    """
    total = 0

    with out_path.open("w", encoding="utf-8") as f:
        for pr in partition_ranges:
            p          = pr["partition"]
            start_off  = pr["start_offset"]
            end_offset = pr["end_offset"]

            # Assign this partition and seek to start offset
            tp = TopicPartition(topic, p, start_off)
            consumer.assign([tp])
            consumer.seek(tp)

            log.info("Consuming partition %d from offset %d …", p, start_off)
            partition_count = 0

            while True:
                msg = consumer.poll(timeout=5.0)

                if msg is None:
                    log.info("Partition %d: poll timeout — assuming end of data.", p)
                    break

                if msg.error():
                    if msg.error().code() == KafkaError._PARTITION_EOF:
                        log.info("Partition %d: reached end of partition.", p)
                    else:
                        log.error("Partition %d: consumer error: %s", p, msg.error())
                    break

                msg_ts_ms = msg.timestamp()[1]  # (TIMESTAMP_TYPE, epoch_ms)

                # Guard: skip messages outside the requested time window
                # (edge case: start offset may point slightly before start_ms)
                if msg_ts_ms < start_ms:
                    continue

                # Stop condition: message timestamp has crossed end boundary
                if msg_ts_ms >= end_ms:
                    log.info("Partition %d: reached end timestamp — stopping.", p)
                    break

                # Stop condition: offset-based end
                if end_offset is not None and msg.offset() >= end_offset:
                    log.info("Partition %d: reached end offset %d — stopping.", p, end_offset)
                    break

                # Parse and enrich the message
                try:
                    payload = json.loads(msg.value().decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    log.warning(
                        "Partition %d offset %d: could not parse JSON (%s) — writing raw.",
                        p, msg.offset(), exc,
                    )
                    payload = {"_raw": msg.value().decode("utf-8", errors="replace")}

                # Wrap with Kafka metadata for traceability
                record = {
                    "_meta": {
                        "topic":     msg.topic(),
                        "partition": msg.partition(),
                        "offset":    msg.offset(),
                        "timestamp": from_epoch_ms(msg_ts_ms),
                        "key":       msg.key().decode("utf-8") if msg.key() else None,
                    },
                    **payload,
                }

                f.write(json.dumps(record) + "\n")
                partition_count += 1

            log.info("Partition %d: wrote %d messages.", p, partition_count)
            total += partition_count

    return total


# ─────────────────────────────────────────────────────────────────────────────
# 5. Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()

    start_ms = to_epoch_ms(args.start, args.tz)
    end_ms   = to_epoch_ms(args.end,   args.tz)

    if end_ms <= start_ms:
        log.error("--end must be after --start.")
        sys.exit(1)

    log.info("Date range : %s → %s", from_epoch_ms(start_ms), from_epoch_ms(end_ms))
    log.info("Topic      : %s", args.topic)
    log.info("Output     : %s", args.out)

    # Build consumer config — merge SSL config with consumer-specific settings
    ssl_config = get_kafka_ssl_config()
    consumer_config = {
        **ssl_config,
        "group.id":           args.group,
        "auto.offset.reset":  "earliest",   # fallback if offsets_for_times returns nothing
        "enable.auto.commit": False,         # read-only — never commit offsets to prod
    }

    consumer = Consumer(consumer_config)

    try:
        partition_ranges = resolve_offsets(consumer, args.topic, start_ms, end_ms)

        if not partition_ranges:
            log.warning("No messages found in the given time range. Nothing to write.")
            sys.exit(0)

        out_path = Path(args.out)
        total = consume_and_dump(consumer, args.topic, partition_ranges, start_ms, end_ms, out_path)

        log.info("─" * 60)
        log.info("Done. %d messages written to %s", total, out_path.resolve())

    finally:
        consumer.close()


if __name__ == "__main__":
    main()
