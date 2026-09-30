#!/usr/bin/env python3
"""
check_s3_backup - Naemon/Nagios plugin to monitor S3 backup status

Checks the backup log file produced by naemon_backup.py and returns:

  OK       (0) - Backup successful and timestamp is within the warning threshold (< 48h)
  WARNING  (1) - Backup successful but timestamp exceeds the warning threshold (> 48h)
  CRITICAL (2) - Backup failed, OR timestamp exceeds the critical threshold (> 72h)
  UNKNOWN  (3) - Cannot read or parse the log file

Usage:
  check_s3_backup.py -f /path/to/backup.log
  check_s3_backup.py -f /path/to/backup.log -w 48 -c 72

Author: NOC Team
"""

import argparse
import os
import re
import sys
from datetime import datetime

# ── Naemon / Nagios exit codes ──────────────────────────────────────────────
OK = 0
WARNING = 1
CRITICAL = 2
UNKNOWN = 3

# ── Default configuration ───────────────────────────────────────────────────
DEFAULT_LOG_FILE = os.path.expanduser("~/backup/log/backup.log")
DEFAULT_WARNING_HOURS = 48
DEFAULT_CRITICAL_HOURS = 72
SUCCESS_MARKER = "BACKUP COMPLETADO EXITOSAMENTE"

# Regex for log lines: [YYYY-MM-DD HH:MM:SS] message
TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")


def parse_timestamp(line):
    """Extract a datetime from a log line, or None."""
    m = TS_RE.match(line)
    if m:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    return None


def read_log(log_file):
    """
    Read and parse the log file.
    Returns (success, backup_timestamp, hostname, error_message) or raises ValueError.
    """
    if not os.path.exists(log_file):
        raise FileNotFoundError(f"Log file not found: {log_file}")

    if os.path.getsize(log_file) == 0:
        raise ValueError("Log file is empty (no backup has been recorded)")

    with open(log_file, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    if not lines or all(l.strip() == "" for l in lines):
        raise ValueError("Log file is empty (no backup has been recorded)")

    success = False
    success_ts = None
    last_ts = None
    hostname = None
    error_msg = ""

    for line in lines:
        ts = parse_timestamp(line)
        if ts:
            last_ts = ts

        if SUCCESS_MARKER in line:
            success = True
            success_ts = ts

        # Extract hostname from the INICIANDO line
        h = re.search(r"INICIANDO BACKUP DE .+ \(([^)]+)\)", line)
        if h:
            hostname = h.group(1)

        # Capture the last ERROR line (for failure messages)
        if "ERROR" in line:
            error_msg = line.strip()

    # Determine which timestamp to use
    if success:
        backup_ts = success_ts or last_ts
    else:
        backup_ts = last_ts

    if backup_ts is None:
        raise ValueError("No valid timestamp found in log file")

    return success, backup_ts, hostname, error_msg


def check_backup(log_file, warning_hours, critical_hours):
    """
    Main check logic. Returns (exit_code, message).
    """
    # ── Try to read and parse the log ────────────────────────────────────────
    try:
        success, backup_ts, hostname, error_msg = read_log(log_file)
    except FileNotFoundError as e:
        return CRITICAL, f"CRITICAL: {e}"
    except ValueError as e:
        return CRITICAL, f"CRITICAL: {e}"
    except Exception as e:
        return UNKNOWN, f"UNKNOWN: Cannot read log file {log_file}: {e}"

    # ── Calculate backup age ─────────────────────────────────────────────────
    now = datetime.now()
    age = now - backup_ts
    age_hours = age.total_seconds() / 3600.0

    # Build the host portion of the output
    host_str = f" [{hostname}]" if hostname else ""
    ts_str = backup_ts.strftime("%Y-%m-%d %H:%M:%S")

    # Performance data: backup_age in hours with thresholds
    # Format: 'label'=valueUOM;warn;crit;min;max
    perfdata = f"backup_age={age_hours:.1f}h;{warning_hours};{critical_hours};0;"

    # ── CRITICAL: backup failed ───────────────────────────────────────────────
    if not success:
        extra = f" | Last error: {error_msg}" if error_msg else ""
        msg = (
            f"CRITICAL: Last backup FAILED{host_str} | "
            f"Last attempt: {ts_str} ({age_hours:.1f}h ago){extra} | {perfdata}"
        )
        return CRITICAL, msg

    # ── CRITICAL: timestamp older than critical threshold ────────────────────
    if age_hours > critical_hours:
        msg = (
            f"CRITICAL: Backup successful but too old{host_str} | "
            f"Last backup: {ts_str} ({age_hours:.1f}h ago, "
            f"threshold: {critical_hours}h) | {perfdata}"
        )
        return CRITICAL, msg

    # ── WARNING: timestamp older than warning threshold ──────────────────────
    if age_hours > warning_hours:
        msg = (
            f"WARNING: Backup successful but aging{host_str} | "
            f"Last backup: {ts_str} ({age_hours:.1f}h ago, "
            f"threshold: {warning_hours}h) | {perfdata}"
        )
        return WARNING, msg

    # ── OK: recent and successful ────────────────────────────────────────────
    msg = (
        f"OK: Backup successful and recent{host_str} | "
        f"Last backup: {ts_str} ({age_hours:.1f}h ago) | {perfdata}"
    )
    return OK, msg


def main():
    parser = argparse.ArgumentParser(
        description="Check S3 backup status from log file (Naemon/Nagios plugin)"
    )
    parser.add_argument(
        "-f", "--log-file",
        default=DEFAULT_LOG_FILE,
        help=f"Path to backup log file (default: {DEFAULT_LOG_FILE})",
    )
    parser.add_argument(
        "-w", "--warning",
        type=float, default=DEFAULT_WARNING_HOURS,
        help=f"Warning threshold in hours (default: {DEFAULT_WARNING_HOURS})",
    )
    parser.add_argument(
        "-c", "--critical",
        type=float, default=DEFAULT_CRITICAL_HOURS,
        help=f"Critical threshold in hours (default: {DEFAULT_CRITICAL_HOURS})",
    )
    args = parser.parse_args()

    exit_code, message = check_backup(args.log_file, args.warning, args.critical)
    print(message)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
