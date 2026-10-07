# S3 Backup — Configuration Backup to AWS S3

> 🌐 [Español](README_ES.md)

Backup script that compresses configurations into a `.tar.gz` file, uploads it to an AWS S3 bucket, sends the result by email, and keeps a local log. Includes a Naemon/Nagios monitoring plugin that checks backup status based on the log.

Designed to be reusable across different host types (Naemon, Grafana, NOC servers, etc.) via a per-host `.conf` configuration file.

## Project Files

| File | .gitignore | Description |
|---|---|---|
| `naemon_backup.py` | No | Main backup script |
| `check_s3_backup.py` | No | Naemon/Nagios monitoring plugin |
| `naemon_config_example.cfg` | No | Example Naemon configuration (command + service) |
| `s3bkp.conf` | **Yes** | Configuration with real environment values |
| `s3bkp.conf.example` | No | Configuration template without sensitive values |
| `S3backup.log` | Yes | Last run log (truncated on each run) |
| `README.md` | No | This documentation (English) |
| `README_ES.md` | No | Spanish documentation |
| `CONTEXT.md` | No | Project context for Toqan sessions |

## Dependencies

- **Python 3.8+**
- **boto3** — `pip install boto3` or `apt install python3-boto3`
- **botocore** — included with boto3

> **Important:** If the server already has `aws-cli` installed via apt (system package), installing `boto3` with `pip install --user` may break the `aws` CLI due to `botocore` version conflicts. In that case, use a **virtual environment**:
> ```bash
> python3 -m venv ~/S3backup/venv
> ~/S3backup/venv/bin/pip install boto3
> # In crontab, use the venv python:
> # 00 22 * * * ~/S3backup/venv/bin/python3 ~/S3backup/naemon_backup.py
> ```
> Or install `boto3` via apt: `sudo apt install python3-boto3`.
- **AWS access** — IAM Role on EC2 or `~/.aws/credentials`
- **sudo NOPASSWD** — the user running the script needs passwordless sudo to copy `retention.dat` (only if `[Retention] enabled = true`)

## Installation

### 1. Copy files to the server

```bash
# Script directory (must match crontab)
mkdir -p /home/noc_user/S3backup
cp naemon_backup.py /home/noc_user/S3backup/
cp s3bkp.conf.example /home/noc_user/S3backup/s3bkp.conf
```

### 1b. Create the log directory

The log must be in a path accessible by both the user running the script and Naemon:

```bash
sudo mkdir -p /var/log/naemon/s3backup
sudo chown noc_user:naemon /var/log/naemon/s3backup
sudo chmod 775 /var/log/naemon/s3backup
```

### 2. Edit configuration

```bash
vi /home/noc_user/S3backup/s3bkp.conf
```

Adjust all values for your environment. See [Configuration](#configuration-s3bkpconf) below.

### 3. Set up crontab

```bash
crontab -e
```

```cron
# BACKUP EXECUTION
00 22 * * * python3 /home/noc_user/S3backup/naemon_backup.py
```

### 4. Install monitoring plugin

```bash
sudo cp check_s3_backup.py /usr/lib/naemon/plugins/noc/
sudo chmod +x /usr/lib/naemon/plugins/noc/check_s3_backup.py
```

### 5. Configure Naemon service

```bash
sudo cp naemon_config_example.cfg /etc/naemon/conf.d/noc/s3_backup.cfg
# Edit host_name, contacts, and log path for your environment
sudo vi /etc/naemon/conf.d/noc/s3_backup.cfg
sudo naemon -v /etc/naemon/naemon.cfg
sudo systemctl reload naemon
```

### 6. Remote monitoring via NRPE (optional)

If the backup runs on a host other than the Naemon server, use NRPE to run the check remotely.

**On the remote host (where the backup runs):**

```bash
# Install NRPE
sudo apt install nagios-nrpe-server

# Install the plugin
sudo cp check_s3_backup.py /usr/lib/naemon/plugins/noc/
sudo chmod +x /usr/lib/naemon/plugins/noc/check_s3_backup.py

# Configure the NRPE command
# Add to /etc/nagios/nrpe.cfg or a file in /etc/nagios/nrpe.d/:
# command[check_s3_backup]=/usr/lib/naemon/plugins/noc/check_s3_backup.py -f /var/log/naemon/s3backup/S3backup.log

# Allow the Naemon server
# Edit allowed_hosts in NRPE config:
# allowed_hosts=127.0.0.1,<NAEMON_SERVER_IP>

# Restart NRPE
sudo systemctl restart nagios-nrpe-server
```

**On the Naemon server:**

```bash
# Test NRPE connection
/usr/lib/naemon/plugins/check_nrpe -H <REMOTE_HOST_IP> -c check_s3_backup
```

See `naemon_config_example.cfg` for the NRPE command and service definition.

## Configuration (`s3bkp.conf`)

The configuration file uses INI format with `configparser`. It must be in the same directory as `naemon_backup.py`. Required sections: `[General]`, `[AWS]`, `[Proxy]`, `[Paths]`, `[Mail]`. Optional: `[Retention]`, `[S3]`, `[Database]`, `[Encrypt]`.

### Sections

#### `[General]` — Required
| Key | Default | Description |
|---|---|---|
| `service_name` | — | Name of the service being backed up. Appears in the log and email subject. E.g.: `Naemon`, `Grafana`, `NOC Server`. |
| `dereference_symlinks` | `true` | `true` = copies the symlink target file (recommended for restore). `false` = preserves the symlink as-is (only works if restored to the same path). |

#### `[AWS]` — Required
| Key | Description |
|---|---|
| `bucket_name` | Target S3 bucket name |
| `s3_prefix` | Prefix (folder) within the bucket. To separate by host, include a subfolder: `backups/naemon/Regional`, `backups/grafana`, etc. |
| `region` | AWS region where the bucket is located |

#### `[Proxy]` — Required
| Key | Description |
|---|---|
| `http_proxy` | HTTP proxy URL (without proxy, no AWS connectivity) |
| `https_proxy` | HTTPS proxy URL |

#### `[Paths]` — Required
| Key | Description |
|---|---|
| `log_file` | Path to the log file (supports `~`). Must be accessible by Naemon. E.g.: `/var/log/naemon/s3backup/S3backup.log` |
| `tmp_dir` | Temporary directory for the `.tar.gz` |
| `tmp_dir_retention` | Temporary directory for `retention.dat`. **Only required if `[Retention] enabled = true`.** If retention is disabled, this can be omitted. |

#### `[Retention]` — Optional
| Key | Default | Description |
|---|---|---|
| `enabled` | `false` | `true` = copies `retention.dat` and includes it in the archive (Naemon hosts only). `false` = skips this routine. If section is absent, default = `false`. |
| `source` | — | Source path of `retention.dat` (copied with sudo). Only required if `enabled = true`. |
| `owner` | — | Owner of the copied file. Only required if `enabled = true`. |
| `group` | — | Group of the copied file. Only required if `enabled = true`. |
| `permissions` | — | Permissions in octal format (e.g.: `660`). Only required if `enabled = true`. |

#### `[Backup_Paths]` — Required (or `[Database_List]` if only backing up DBs)
| Key | Description |
|---|---|
| `path` | Multiline list of paths to include in the backup, one per line, indented. **Optional** if `[Database] enabled = true` (DB-only backup). At least `[Backup_Paths]` or `[Database_List]` with databases is required. |

> `retention.dat` is automatically included from `tmp_dir_retention` if `[Retention] enabled = true`. No need to list it in `[Backup_Paths]`.

#### `[Mail]` — Required
| Key | Description |
|---|---|
| `smtp_host` | SMTP server (usually `localhost`) |
| `from` | Email sender |
| `to` | Email recipient |

#### `[Database]` — Optional
| Key | Default | Description |
|---|---|---|
| `enabled` | `false` | `true` = enables database backup. `false` = skips. If section is absent, default `false`. |
| `container` | — | Docker container name running MySQL/MariaDB. If empty, runs `mysqldump` directly on the host. |
| `db_host` | — | Database host for `mysqldump` (e.g.: `127.0.0.1` inside Docker). If empty, uses the Unix socket by default. |
| `credentials_file` | — | Path to the INI credentials file (one section per DB). Required if `enabled = true`. |
| `dump_dir` | — | Temporary directory for dumps. Required if `enabled = true`. |
| `mysqldump_options` | — | Extra options for `mysqldump` (added to defaults: `--single-transaction --routines --triggers --quick`). |
| `dump_timeout` | `300` | Dump timeout in seconds. |
| `min_free_space_mb` | `1024` | Minimum free space in `dump_dir` before each dump (in MB). |

#### `[Database_List]` — Required if `[Database] enabled = true`
| Key | Description |
|---|---|
| `db` | Multiline list of database names, one per line, indented. |

#### `[Encrypt]` — Optional
| Key | Default | Description |
|---|---|---|
| `enabled` | `false` | `true` = enables encryption of sensitive archives. `false` = no encryption. |
| `passphrase_file` | — | Path to the credentials file containing the passphrase (`[Encryption]` section). Required if `enabled = true`. |

#### `[Encrypt_Paths]` — Required if `[Encrypt] enabled = true` and no `[Encrypt_Databases]`
| Key | Description |
|---|---|
| `path` | Directories or files to back up in a separate encrypted archive. Cannot also be in `[Backup_Paths]`. |

#### `[Encrypt_Databases]` — Required if `[Encrypt] enabled = true` and no `[Encrypt_Paths]`
| Key | Description |
|---|---|
| `db` | Database names whose dump will be encrypted before uploading. Must also be in `[Database_List]`. |

#### `[S3]` — Optional
| Key | Default | Description |
|---|---|---|
| `max_retries` | `5` | Maximum upload attempts (1 initial + N retries) |

### S3 bucket organization

The `s3_prefix` controls the path within the bucket. To keep backups separated by host/type, configure the prefix with subfolders:

| Host | `s3_prefix` | Resulting S3 key |
|---|---|---|
| Naemon Regional | `backups/naemon/Regional` | `backups/naemon/Regional/host_YYYY-MM-DD.tar.gz` |
| Naemon Cross | `backups/naemon/Cross` | `backups/naemon/Cross/host_YYYY-MM-DD.tar.gz` |
| Naemon NR | `backups/naemon/NR` | `backups/naemon/NR/host_YYYY-MM-DD.tar.gz` |
| Grafana | `backups/grafana` | `backups/grafana/host_YYYY-MM-DD.tar.gz` |

## How the script works (`naemon_backup.py`)

### Execution flow

1. **Loads and validates configuration** from `s3bkp.conf`. If any required value is missing, the script exits with an error before doing anything. Validation is conditional: `[Retention]` fields and `tmp_dir_retention` are only required if `enabled = true`.
2. **Cleans temporary directories** to remove leftovers from a previous interrupted run.
3. **Truncates the log** — the log always contains only the last run.
4. **Copies `retention.dat`** (if `enabled = true`) from `/var/lib/naemon/` to the temp directory using `sudo` with `subprocess.run(check=True)`. If it fails, the script stops.
5. **Creates the `.tar.gz` archive** with all configured paths (if `[Backup_Paths]` is present or retention is enabled). If retention is enabled, also includes `retention.dat` from the temp directory. Non-existent paths are skipped with a `WARNING` in the log. Logs the file size. If no `[Backup_Paths]` and no retention → skips config backup and goes straight to DBs.
6. **Uploads to S3** with configurable retries (exponential backoff). Only retries transient errors (timeouts, 5xx, throttling); permanent errors (AccessDenied, NoSuchBucket) are not retried.
7. **Verifies the upload** with `head_object` comparing the object size in S3 with the local file.
8. **Logs `=== BACKUP COMPLETADO EXITOSAMENTE ===`** — this is the marker the monitoring plugin looks for. Only logged if everything succeeded (configs + encrypted configs + all DBs).
9. **Sends the email** with the log contents. If the email fails, it is logged as `WARNING:` (does not affect the backup status).
10. **Cleans temporaries** again (guaranteed by `try/finally`, runs even if something fails mid-run).
11. **Database backup** (if `enabled = true`) — for each DB: pre-flight checks, `mysqldump`, validation, compression, encryption (if applicable), upload to S3 and verification. Sequential dumps (one at a time).
12. **Encrypted configs** (if `[Encrypt] enabled = true` and `[Encrypt_Paths]` not empty) — separate archive, OpenSSL encryption, delete plaintext, upload `.enc`.
13. **Exit code** — `0` if everything succeeded, `1` if anything failed. The success marker is only logged if everything is OK.

### Backup log

The log is truncated at the start of each run. Format:

```
[2026-09-27 23:50:01] === INICIANDO BACKUP DE Naemon (naemon_server_1) ===
[2026-09-27 23:50:05] Agregado al archive: /etc/naemon
[2026-09-27 23:50:05] Agregado al archive: /etc/thruk/cgi.cfg
[2026-09-27 23:50:06] Agregado al archive: /usr/lib/nagios/plugins
[2026-09-27 23:50:06] Agregado al archive: /var/log/naemon
[2026-09-27 23:50:06] Agregado al archive: /home/noc_user/naemon_retention/retention.dat
[2026-09-27 23:50:07] Archivo creado: naemon_server_1_2026-09-27.tar.gz (18.2 MB)
[2026-09-27 23:50:10] Backup subido y verificado en S3: backups/naemon/Regional/naemon_server_1_2026-09-27.tar.gz (19084051 bytes)
[2026-09-27 23:50:10] === INICIANDO BACKUP DE DATABASES (2 bases) ===
[2026-09-27 23:50:12] Dump creado: noctools_prod (12.3 MB)
[2026-09-27 23:50:12] Agregado al archive: noctools_prod.sql
[2026-09-27 23:50:14] Backup subido y verificado en S3: backups/naemon/Regional/databases/naemon_server_1_2026-09-27_noctools_prod.tar.gz (12897264 bytes)
[2026-09-27 23:50:16] Dump creado: grafana (45.6 MB)
[2026-09-27 23:50:16] Agregado al archive: grafana.sql
[2026-09-27 23:50:19] Backup subido y verificado en S3: backups/naemon/Regional/databases/naemon_server_1_2026-09-27_grafana.tar.gz (47823872 bytes)
[2026-09-27 23:50:19] === BACKUP DE DATABASES: 2/2 OK ===
[2026-09-27 23:50:19] Archivo seguro creado: naemon_server_1_2026-09-27_secure.tar.gz (0.1 MB)
[2026-09-27 23:50:19] Archive encriptado: naemon_server_1_2026-09-27_secure.tar.gz.enc
[2026-09-27 23:50:19] Backup subido y verificado en S3: backups/noctools/naemon_server_1_2026-09-27_secure.tar.gz.enc (102456 bytes)
[2026-09-27 23:50:19] Limpiando archivos temporales
[2026-09-27 23:50:19] === BACKUP COMPLETADO EXITOSAMENTE ===
```

> **Note:** Log messages are in Spanish by design (operator-facing). The log format, prefixes, and markers are fixed and used by the monitoring plugin.

### Log prefixes

| Prefix | Meaning | Affects monitoring? |
|---|---|---|
| `=== INICIANDO BACKUP DE {service_name} ({host}) ===` | Run start | No |
| `Agregado al archive: ...` | Each path included in the backup | No |
| `Archivo creado: ...` | Archive info (name + size) | No |
| `Backup subido y verificado en S3: ...` | Successful upload | No |
| `=== BACKUP COMPLETADO EXITOSAMENTE ===` | **Success marker** — the plugin looks for this line | **Yes — determines OK** |
| `=== INICIANDO BACKUP DE DATABASES ===` | DB backup start | No |
| `Dump creado: ...` | DB dump created | No |
| `Backup subido y verificado en S3: ...` | DB dump uploaded OK | No |
| `=== BACKUP DE DATABASES: N/N OK ===` | DB backup summary | No |
| `ERROR:` | Backup failure (retention, tar, S3, DB dump) | **Yes — determines CRITICAL** |
| `Archivo seguro creado: ...` | Sensitive archive created | No |
| `Archive encriptado: ...` | Encryption OK | No |
| `WARNING:` | Non-critical failure (email, path not found) | No — backup remains OK |

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Backup successful |
| `1` | Backup failed (any step) |

## How monitoring works (`check_s3_backup.py`)

### Status logic

| Condition | Status | Exit Code |
|---|---|---|
| Backup successful + timestamp < 48h | 🟢 OK | 0 |
| Backup successful + timestamp between 48h and 72h | 🟡 WARNING | 1 |
| Backup failed (any age) | 🔴 CRITICAL | 2 |
| Backup successful + timestamp > 72h | 🔴 CRITICAL | 2 |
| Log file missing or empty | 🔴 CRITICAL | 2 |
| Log file unreadable | ⚪ UNKNOWN | 3 |

### Evaluation order

1. Backup failed? → **CRITICAL** (regardless of age)
2. Timestamp > 72h? → **CRITICAL** (even if successful, too old)
3. Timestamp > 48h? → **WARNING** (successful but aging)
4. All OK → **OK**

### Debug mode (`--debug`)

The `--debug` flag prints detailed progress to stdout in real time. Includes:

- Loaded configuration values (service_name, bucket, proxy, paths, etc.)
- Each `sudo` command executed in `copy_retention()`
- Each process step (log truncation, archive creation, S3 connection, verification, email)
- Cleanup verification (confirms temporaries were deleted)
- Closing message with the overall run result
- Everything that goes to the log is also printed to screen

Without `--debug`, the script runs silently (ideal for cron).

```bash
# Normal mode (cron, silent)
python3 /home/noc_user/S3backup/naemon_backup.py

# Debug mode (manual, verbose)
python3 /home/noc_user/S3backup/naemon_backup.py --debug

# Debug mode with custom config
python3 /home/noc_user/S3backup/naemon_backup.py --debug --config /tmp/test.conf
```

#### Debug colors

Colors are applied only in a terminal (TTY detection). They do not appear in the log or email.

| Color | Type | Examples |
|---|---|---|
| **Bold red** | Critical error | `ERROR: Falló copy_retention...` |
| **Bold green** | Successful run end | `=== BACKUP COMPLETADO EXITOSAMENTE ===` |
| **Bold cyan** | Run start | `=== INICIANDO BACKUP DE Naemon (host) ===` |
| **Bold yellow** | End with warnings | `=== BACKUP FINALIZADO CON WARNINGS ===` |
| Red | — | *(errors are always bold red)* |
| Yellow | Warnings | `WARNING: Ruta no encontrada...` |
| Green | Step success | `Agregado al archive:`, `Archivo creado:`, `Cleanup OK:`, `copy_retention: OK`, `Verificación OK:` |
| Cyan | In-progress actions | `Cargando`, `Ejecutando:`, `Subiendo`, `Conectando a S3`, `Limpiando` |
| Default | Data / variables | `service_name = Naemon`, `bucket_name = ...` |

#### Closing message

At the end of the run (in `--debug`), a summary message is printed:

| Message | Color | Condition |
|---|---|---|
| `=== BACKUP FINALIZADO SIN ERRORES ===` | Bold green | Backup successful, no warnings |
| `=== BACKUP FINALIZADO CON WARNINGS ===` | Bold yellow | Backup successful, but warnings occurred (missing path, email failed, cleanup failed) |
| `=== BACKUP FINALIZADO CON ERRORES ===` | Bold red | Backup failed at some step |

#### Cleanup verification

The script verifies that temporary directories are deleted correctly:

- If cleanup succeeds → `Cleanup OK: /tmp/naemon_backup` (only in `--debug`)
- If cleanup fails → `WARNING: No se pudo eliminar /tmp/naemon_backup` (always in log and email)

### Plugin usage

```
usage: check_s3_backup.py [-h] [-f LOG_FILE] [-w WARNING] [-c CRITICAL]

  -f, --log-file    Path to the backup log (default: ~/backup/log/backup.log)
  -w, --warning     Warning threshold in hours (default: 48)
  -c, --critical    Critical threshold in hours (default: 72)
  -h, --help        Show help
```

The plugin extracts the hostname from the log using a generic regex `INICIANDO BACKUP DE .+ (hostname)` — works with any `service_name`.

### Performance data

The plugin emits performance data compatible with PNP4Nagios / Grafana:

```
backup_age=19.4h;48;72;0;
```

## Database backup

The script supports MySQL/MariaDB database backup via `mysqldump` (direct or via Docker) with separate uploads to S3.

### Architecture

Each DB generates its own independent `.tar.gz`, separate from the config archive:

| Archive | Contents | S3 key |
|---|---|---|
| `hostname_date.tar.gz` | Configs + retention.dat | `s3_prefix/hostname_date.tar.gz` |
| `hostname_date_dbname.tar.gz` | One compressed `.sql` dump | `s3_prefix/databases/hostname_date_dbname.tar.gz` |

This allows restoring configs or a specific DB without downloading the entire backup.

### Credentials file

Each DB's credentials are configured in a separate INI file (path defined in `credentials_file`):

```ini
[noctools_prod]
user = dbuser
password = dbpass

[grafana]
user = grafana_user
password = grafana_pass
```

Recommended: `chmod 600` on the credentials file.

### Pre-flight checks

Before running dumps, the script verifies:

1. The credentials file exists
2. Docker is available (if using container) or `mysqldump` is in PATH (if direct mode)
3. The container is running (if using Docker)
4. Each DB in `[Database_List]` has credentials
5. Sufficient disk space in `dump_dir`

### Dump validation

After each `mysqldump`:

- The `.sql` file is not empty
- The header starts with `-- MySQL dump` or `-- MariaDB dump` (validates a real dump)
- The `.sql` is deleted immediately after compression (frees disk space)

### Upload verification

All uploads (configs + DBs) are verified with:

- `head_object` — the object exists in S3
- Size comparison — local vs remote
- MD5 checksum — if the ETag is not multipart (small files)

## Archive encryption

The script supports optional OpenSSL encryption (AES-256-CBC + PBKDF2) for sensitive archives.

### How it works

- **Sensitive configs** (`[Encrypt_Paths]`): a separate archive `hostname_date_secure.tar.gz` is created, encrypted to `.tar.gz.enc`, the plaintext `.tar.gz` is deleted, and the `.enc` is uploaded to S3.
- **Sensitive DBs** (`[Encrypt_Databases]`): each dump is compressed to `.tar.gz`, encrypted to `.tar.gz.enc`, the plaintext `.tar.gz` is deleted, and the `.enc` is uploaded to S3.
- **Normal configs** (`[Backup_Paths]`): uploaded unencrypted (same as always).
- **DBs not listed** in `[Encrypt_Databases]`: uploaded unencrypted.

### Passphrase

The passphrase is read from the credentials file (same as DBs), `[Encryption]` section:

```ini
[Encryption]
passphrase = my-secret-passphrase
```

The passphrase is passed to OpenSSL via **stdin** — never touches disk or appears in `ps`.

### Pre-flight checks

Before encrypting, the script verifies:

1. `openssl` exists in PATH
2. The passphrase file exists
3. The `[Encryption]` section exists in the file
4. The passphrase is not empty

### Configuration validation

- If `[Encrypt] enabled = true` but no paths or DBs in `[Encrypt_Paths]`/`[Encrypt_Databases]` → **ERROR** (backup fails, CRITICAL in the monitor).
- If a path is in both `[Backup_Paths]` and `[Encrypt_Paths]` → **ERROR** (the script does not start).

### Restore

| Type | Command |
|---|---|
| Normal configs | `tar xzf hostname_date.tar.gz` |
| Encrypted configs | `openssl enc -d -aes-256-cbc -pbkdf2 -pass stdin < hostname_date_secure.tar.gz.enc \| tar xzf -` |
| Encrypted DB | `openssl enc -d -aes-256-cbc -pbkdf2 -pass stdin < hostname_date_dbname.tar.gz.enc \| tar xzf -` |
| Unencrypted DB | `tar xzf hostname_date_dbname.tar.gz` |

## Edge cases

1. **Log file missing** → CRITICAL: "Log file not found"
2. **Log file empty** → CRITICAL: "Log file is empty"
3. **Interrupted backup** (only the INICIANDO line, no COMPLETADO or ERROR) → CRITICAL: "Last backup FAILED"
4. **Email fails after successful backup** → OK: the backup completed, the email error is logged as `WARNING:`
5. **Backup path does not exist** → WARNING in the log, backup continues with remaining paths
6. **retention.dat not found in temp destination** → WARNING in the log, backup continues without that file
7. **Host without Naemon (retention disabled)** → skips `copy_retention()`, backup only includes `[Backup_Paths]`
8. **Script interrupted by SIGKILL/OOM** → temporaries are cleaned at the start of the next run
9. **Transient S3 failure** → up to 5 attempts with exponential backoff before declaring failure
10. **Symlinks in backup paths** → if `dereference_symlinks = true` (default), the target file is copied instead of the link. If `false`, the symlink is preserved (only works if restored to the same path)
11. **Docker container down** → pre-flight check detects and reports before attempting any dump
12. **Empty or corrupt dump** → header + size validation detects invalid dumps
13. **Insufficient space in dump_dir** → pre-flight check verifies minimum free space before each dump
14. **mysqldump timeout** → if the dump takes longer than the configured timeout, the process is killed and an ERROR is logged
15. **DB dump fails** → the script continues with remaining DBs, but the final result is CRITICAL (no success marker)
16. **Encryption enabled with no paths or DBs** → ERROR: backup fails (CRITICAL in the monitor)
17. **Duplicate path between [Backup_Paths] and [Encrypt_Paths]** → ERROR: the script does not start
18. **OpenSSL not available** → encryption pre-flight fails, backup fails (CRITICAL)
19. **Empty passphrase or missing [Encryption] section** → encryption pre-flight fails, backup fails (CRITICAL)
20. **Password with special characters** (`%`, `#`, etc.) → the credentials file uses `RawConfigParser`, characters are read literally without interpolation
21. **MariaDB** → the script accepts both `-- MySQL dump` and `-- MariaDB dump` headers in dump validation
22. **Log directory does not exist** → the script creates it automatically before truncating the log
23. **Coexistence with system `aws-cli`** → if there is a `botocore` conflict, use a venv (see Dependencies)

## S3 backup retention

Retention of old backups in S3 is managed with a **bucket lifecycle policy**, not from the script. Configure in the AWS Console:

- Bucket → Management → Lifecycle rules
- Rule: expire objects under `backups/` after N days (recommended: 30-90)

## Sudoers

The user running the script needs passwordless sudo to copy `retention.dat` (only if `[Retention] enabled = true`):

```
noc_user ALL=(ALL) NOPASSWD: ALL
```

Or more restrictive, if preferred:

```
noc_user ALL=(root) NOPASSWD: /usr/bin/cp /var/lib/naemon/retention.dat /home/noc_user/naemon_retention/
noc_user ALL=(root) NOPASSWD: /usr/bin/chown naemon:naemon /home/noc_user/naemon_retention/retention.dat
noc_user ALL=(root) NOPASSWD: /usr/bin/chmod 660 /home/noc_user/naemon_retention/retention.dat
```

If `enabled = false`, sudo is not needed for the script.
