#!/usr/bin/env python3
"""
naemon_backup.py — Backup de configuraciones y bases de datos a S3

Dependencias:
- Python 3.8+
- boto3          (pip install boto3 o apt install python3-boto3)
- botocore       (incluido con boto3)
- Acceso a AWS   (IAM Role en EC2 o ~/.aws/credentials)
- sudo NOPASSWD  (para copiar retention.dat de /var/lib/naemon/ si está habilitado)
- mysqldump      (en el host o dentro del container Docker, si DB está habilitado)
- docker         (si se usa container para mysqldump)

Uso:
  python3 naemon_backup.py                     # usa s3bkp.conf en el mismo directorio
  python3 naemon_backup.py --config /ruta/s3bkp.conf
  python3 naemon_backup.py --debug              # modo debug: imprime a stdout todo el progreso
  python3 naemon_backup.py --debug --config /tmp/test.conf

Autor: NOC Team
"""

import argparse
import configparser
import datetime
import hashlib
import os
import shutil
import smtplib
import subprocess
import sys
import tarfile
from email.mime.text import MIMEText
from pathlib import Path

import boto3
from botocore.config import Config as BotoConfig

# === CONSTANTES ===
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_FILE = os.path.join(SCRIPT_DIR, "s3bkp.conf")
SUCCESS_MARKER = "=== BACKUP COMPLETADO EXITOSAMENTE ==="
HOSTNAME = os.uname().nodename.split('.')[0]
DATE = datetime.datetime.now().strftime("%Y-%m-%d")
SMTP_TIMEOUT = 30
ARCHIVE_NAME = f"{HOSTNAME}_{DATE}.tar.gz"

# === COLORES ANSI (solo stdout, nunca en log ni email) ===
ANSI_RED = '\033[31m'
ANSI_GREEN = '\033[32m'
ANSI_YELLOW = '\033[33m'
ANSI_CYAN = '\033[36m'
ANSI_BOLD = '\033[1m'
ANSI_RESET = '\033[0m'

# === GLOBALES (cargados desde el .conf) ===
SERVICE_NAME = None
BUCKET_NAME = None
S3_PREFIX = None
AWS_REGION = None
S3_MAX_RETRIES = 5
LOG_FILE = None
TMP_DIR = None
TMP_DIR_RETENTION = None
RETENTION_ENABLED = False
RETENTION_SOURCE = None
RETENTION_OWNER = None
RETENTION_GROUP = None
RETENTION_PERMS = None
BACKUP_PATHS = []
SMTP_HOST = None
MAIL_FROM = None
MAIL_TO = None
DEREFERENCE_SYMLINKS = True
HAD_WARNINGS = False
DEBUG_MODE = False
DUMP_DIR = None
DB_ENABLED = False
DB_CONTAINER = None
DB_HOST = ""
DB_CREDENTIALS_FILE = None
DB_LIST = []
MYSQLDUMP_OPTIONS = ""
DUMP_TIMEOUT = 300
MIN_FREE_SPACE_MB = 1024
MYSQLDUMP_DEFAULTS = "--single-transaction --routines --triggers --quick"
ENCRYPT_ENABLED = False
ENCRYPT_PASSPHRASE_FILE = None
ENCRYPT_PASSPHRASE = None
ENCRYPT_PATHS = []
ENCRYPT_DATABASES = []
# ─── Utilidades ─────────────────────────────────────────────────────────────

def colorize(line):
    """
    Aplica color ANSI a una línea según su contenido.
    Solo si stdout es una terminal real (TTY). Nunca en archivos ni pipes.

    Jerarquía visual:
      Bold red    — error crítico (detiene el backup)
      Bold green  — fin exitoso del run / cierre sin errores
      Bold yellow — cierre con warnings
      Bold cyan   — inicio del run (header)
      Yellow      — warnings
      Green       — éxito de paso
      Cyan        — acciones en progreso
      Default     — datos / variables
    """
    if not sys.stdout.isatty():
        return line

    # Prioridad: SUMMARY > ERROR > WARNING > END > START > SUCCESS > ACTION > default
    if "BACKUP FINALIZADO CON ERRORES ===" in line:
        return f"{ANSI_BOLD}{ANSI_RED}{line}{ANSI_RESET}"
    elif "BACKUP FINALIZADO CON WARNINGS ===" in line:
        return f"{ANSI_BOLD}{ANSI_YELLOW}{line}{ANSI_RESET}"
    elif "BACKUP FINALIZADO SIN ERRORES ===" in line:
        return f"{ANSI_BOLD}{ANSI_GREEN}{line}{ANSI_RESET}"
    elif "ERROR:" in line:
        return f"{ANSI_BOLD}{ANSI_RED}{line}{ANSI_RESET}"
    elif "WARNING:" in line:
        return f"{ANSI_YELLOW}{line}{ANSI_RESET}"
    elif SUCCESS_MARKER in line:
        return f"{ANSI_BOLD}{ANSI_GREEN}{line}{ANSI_RESET}"
    elif "INICIANDO BACKUP" in line:
        return f"{ANSI_BOLD}{ANSI_CYAN}{line}{ANSI_RESET}"
    elif any(pattern in line for pattern in [
        "Agregado al archive:",
        "Archivo creado:",
        "Backup subido y verificado",
        "copy_retention: OK",
        "Validación de configuración OK",
        "Verificación OK:",
        "Email enviado correctamente",
        "Backup completado exitosamente",
        "Cleanup OK:",
        "Dump creado:",
        "DB dump subido y verificado",
        "BACKUP DE DATABASES:",
        "Pre-flight OK",
        "Espacio en disco OK",
        "SQL temporal eliminado",
        "Archivo seguro creado:",
        "Archive encriptado:",
        "Archive plano eliminado:",
        "Pre-flight encriptación OK",
    ]):
        return f"{ANSI_GREEN}{line}{ANSI_RESET}"
    elif any(pattern in line for pattern in [
        "Cargando configuración",
        "Eliminando temporales",
        "Ejecutando:",
        "Ejecutando cleanup",
        "Creando archive",
        "Conectando a S3",
        "Subiendo ",
        "Verificando upload",
        "Enviando email",
        "Truncando log",
        "Limpiando",
        "Retention habilitado",
        "Retention deshabilitado",
        "copy_retention:",
        "Pre-flight:",
        "Verificando espacio",
        "Ejecutando mysqldump",
        "Pre-flight encriptación:",
        "Encriptando",
    ]):
        return f"{ANSI_CYAN}{line}{ANSI_RESET}"
    else:
        return line
def debug(msg):
    """Imprime un mensaje a stdout solo si --debug está activo. No escribe al log."""
    if DEBUG_MODE:
        ts = datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
        line = f"{ts} [DEBUG] {msg}"
        print(colorize(line))
def log(msg):
    """
    Escribe un mensaje con timestamp al archivo de log (sin color).
    Si --debug, también lo imprime a stdout (con color si es TTY).
    """
    global HAD_WARNINGS
    if msg.startswith("WARNING:"):
        HAD_WARNINGS = True
    log_path = Path(LOG_FILE)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.touch(exist_ok=True)
    except Exception as e:
        print(f"ERROR: No se pudo preparar el archivo de log: {e}")
        return
    ts = datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
    line = f"{ts} {msg}"
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    if DEBUG_MODE:
        print(colorize(line))
def cleanup_temp():
    """
    Elimina los directorios temporales y verifica el resultado.
    Cleanup OK → solo debug. Si falla → WARNING en el log.
    """
    debug(f"Eliminando temporales: {TMP_DIR}...")
    shutil.rmtree(TMP_DIR, ignore_errors=True)
    if os.path.exists(TMP_DIR):
        log(f"WARNING: No se pudo eliminar {TMP_DIR}")
    else:
        debug(f"Cleanup OK: {TMP_DIR}")

    if TMP_DIR_RETENTION is not None:
        debug(f"Eliminando temporales: {TMP_DIR_RETENTION}...")
        shutil.rmtree(TMP_DIR_RETENTION, ignore_errors=True)
        if os.path.exists(TMP_DIR_RETENTION):
            log(f"WARNING: No se pudo eliminar {TMP_DIR_RETENTION}")
        else:
            debug(f"Cleanup OK: {TMP_DIR_RETENTION}")

    if DUMP_DIR is not None:
        debug(f"Eliminando temporales: {DUMP_DIR}...")
        shutil.rmtree(DUMP_DIR, ignore_errors=True)
        if os.path.exists(DUMP_DIR):
            log(f"WARNING: No se pudo eliminar {DUMP_DIR}")
        else:
            debug(f"Cleanup OK: {DUMP_DIR}")
def calculate_md5(filepath, chunk_size=8192):
    """Calcula el hash MD5 de un archivo leyendo en chunks."""
    md5 = hashlib.md5()
    with open(filepath, 'rb') as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            md5.update(chunk)
    return md5.hexdigest()
# ─── Carga y validación de configuración ────────────────────────────────────

def load_config(config_file):
    """Carga y valida el archivo de configuración INI."""
    global SERVICE_NAME
    global BUCKET_NAME, S3_PREFIX, AWS_REGION, S3_MAX_RETRIES
    global LOG_FILE, TMP_DIR, TMP_DIR_RETENTION
    global RETENTION_ENABLED, RETENTION_SOURCE, RETENTION_OWNER
    global RETENTION_GROUP, RETENTION_PERMS
    global BACKUP_PATHS, SMTP_HOST, MAIL_FROM, MAIL_TO
    global DEREFERENCE_SYMLINKS
    global DUMP_DIR, DB_ENABLED, DB_CONTAINER, DB_CREDENTIALS_FILE, DB_HOST
    global DB_LIST, MYSQLDUMP_OPTIONS, DUMP_TIMEOUT, MIN_FREE_SPACE_MB
    global ENCRYPT_ENABLED, ENCRYPT_PASSPHRASE_FILE, ENCRYPT_PASSPHRASE
    global ENCRYPT_PATHS, ENCRYPT_DATABASES

    debug(f"Cargando configuración desde: {config_file}")

    if not os.path.exists(config_file):
        print(f"ERROR: Archivo de configuración no encontrado: {config_file}")
        sys.exit(1)

    cfg = configparser.RawConfigParser()
    cfg.read(config_file)

    # ── Determinar flags condicionales ──────────────────────────────────────
    RETENTION_ENABLED = cfg.getboolean('Retention', 'enabled', fallback=False)
    DB_ENABLED = cfg.getboolean('Database', 'enabled', fallback=False)
    ENCRYPT_ENABLED = cfg.getboolean('Encrypt', 'enabled', fallback=False)
    debug(f"Retention enabled = {RETENTION_ENABLED}")
    debug(f"Database enabled = {DB_ENABLED}")
    debug(f"Encrypt enabled = {ENCRYPT_ENABLED}")

    # ── Construir lista de valores obligatorios dinámicamente ───────────────
    required = {
        'General':    ['service_name'],
        'AWS':        ['bucket_name', 's3_prefix', 'region'],
        'Proxy':      ['http_proxy', 'https_proxy'],
        'Mail':       ['smtp_host', 'from', 'to'],
    }

    if RETENTION_ENABLED:
        required['Paths'] = ['log_file', 'tmp_dir', 'tmp_dir_retention']
        required['Retention'] = ['source', 'owner', 'group', 'permissions']
    else:
        required['Paths'] = ['log_file', 'tmp_dir']

    if DB_ENABLED:
        required['Database'] = ['credentials_file', 'dump_dir']
        required['Database_List'] = ['db']

    if ENCRYPT_ENABLED:
        required['Encrypt'] = ['passphrase_file']

    # ── Validar secciones y valores obligatorios ─────────────────────────────
    missing = []
    for section, keys in required.items():
        if not cfg.has_section(section):
            missing.append(f"[{section}] (sección completa)")
        else:
            for key in keys:
                val = cfg.get(section, key, fallback='').strip()
                if not val:
                    missing.append(f"[{section}] {key}")

    if missing:
        print("ERROR: Faltan valores de configuración requeridos:")
        for m in missing:
            print(f"  - {m}")
        print(f"Archivo: {config_file}")
        sys.exit(1)

    debug("Validación de configuración OK")

    # ── Cargar valores ──────────────────────────────────────────────────────
    SERVICE_NAME = cfg.get('General', 'service_name')
    DEREFERENCE_SYMLINKS = cfg.getboolean('General', 'dereference_symlinks', fallback=True)
    BUCKET_NAME = cfg.get('AWS', 'bucket_name')
    S3_PREFIX = cfg.get('AWS', 's3_prefix')
    AWS_REGION = cfg.get('AWS', 'region')
    S3_MAX_RETRIES = cfg.getint('S3', 'max_retries', fallback=5)

    LOG_FILE = os.path.expanduser(cfg.get('Paths', 'log_file'))
    TMP_DIR = os.path.expanduser(cfg.get('Paths', 'tmp_dir'))

    if RETENTION_ENABLED:
        TMP_DIR_RETENTION = os.path.expanduser(cfg.get('Paths', 'tmp_dir_retention'))
        RETENTION_SOURCE = cfg.get('Retention', 'source')
        RETENTION_OWNER = cfg.get('Retention', 'owner')
        RETENTION_GROUP = cfg.get('Retention', 'group')
        RETENTION_PERMS = cfg.get('Retention', 'permissions')

    if DB_ENABLED:
        DB_CONTAINER = cfg.get('Database', 'container', fallback='').strip()
        DB_HOST = cfg.get('Database', 'db_host', fallback='').strip()
        DB_CREDENTIALS_FILE = os.path.expanduser(cfg.get('Database', 'credentials_file'))
        DUMP_DIR = os.path.expanduser(cfg.get('Database', 'dump_dir'))
        MYSQLDUMP_OPTIONS = cfg.get('Database', 'mysqldump_options', fallback='').strip()
        DUMP_TIMEOUT = cfg.getint('Database', 'dump_timeout', fallback=300)
        MIN_FREE_SPACE_MB = cfg.getint('Database', 'min_free_space_mb', fallback=1024)

        # Database_List — valor multilinea, una DB por línea
        raw_dbs = cfg.get('Database_List', 'db', fallback='')
        DB_LIST = [d.strip() for d in raw_dbs.strip().splitlines() if d.strip()]
        if not DB_LIST:
            print("ERROR: No se han configurado bases de datos en [Database_List]")
            sys.exit(1)

    # ── Cargar configuración de encriptación ────────────────────────────────
    if ENCRYPT_ENABLED:
        ENCRYPT_PASSPHRASE_FILE = os.path.expanduser(cfg.get('Encrypt', 'passphrase_file'))

        # Encrypt_Paths — rutas a encriptar (archive separado)
        raw_enc_paths = cfg.get('Encrypt_Paths', 'path', fallback='')
        ENCRYPT_PATHS = [os.path.expanduser(p.strip())
                         for p in raw_enc_paths.strip().splitlines() if p.strip()]

        # Encrypt_Databases — DBs a encriptar
        raw_enc_dbs = cfg.get('Encrypt_Databases', 'db', fallback='')
        ENCRYPT_DATABASES = [d.strip() for d in raw_enc_dbs.strip().splitlines() if d.strip()]

        # Validar que haya algo que encriptar
        if not ENCRYPT_PATHS and not ENCRYPT_DATABASES:
            print("ERROR: Encriptación habilitada pero no hay paths ni DBs en [Encrypt_Paths] o [Encrypt_Databases]")
            sys.exit(1)

        # Validar overlap entre Backup_Paths y Encrypt_Paths
        # (necesitamos cargar Backup_Paths primero para comparar)

    # Backup_Paths — valor multilinea, una ruta por línea
    raw = cfg.get('Backup_Paths', 'path', fallback='')
    BACKUP_PATHS = [os.path.expanduser(p.strip())
                    for p in raw.strip().splitlines() if p.strip()]
    if not BACKUP_PATHS and not DB_ENABLED:
        print("ERROR: No se han configurado rutas en [Backup_Paths] ni bases de datos en [Database_List]")
        sys.exit(1)
    # Validar overlap entre Backup_Paths y Encrypt_Paths
    if ENCRYPT_ENABLED and ENCRYPT_PATHS:
        overlap = set(BACKUP_PATHS) & set(ENCRYPT_PATHS)
        if overlap:
            print("ERROR: Paths duplicados entre [Backup_Paths] y [Encrypt_Paths]:")
            for p in overlap:
                print(f"  - {p}")
            sys.exit(1)

    SMTP_HOST = cfg.get('Mail', 'smtp_host')
    MAIL_FROM = cfg.get('Mail', 'from')
    MAIL_TO = cfg.get('Mail', 'to')

    # Proxy — obligatorio (sin proxy no hay conexión a AWS)
    os.environ['HTTP_PROXY'] = cfg.get('Proxy', 'http_proxy')
    os.environ['HTTPS_PROXY'] = cfg.get('Proxy', 'https_proxy')

    # ── Debug de valores cargados ───────────────────────────────────────────
    debug(f"service_name  = {SERVICE_NAME}")
    debug(f"dereference_symlinks = {DEREFERENCE_SYMLINKS}")
    debug(f"bucket_name   = {BUCKET_NAME}")
    debug(f"s3_prefix     = {S3_PREFIX}")
    debug(f"region        = {AWS_REGION}")
    debug(f"log_file      = {LOG_FILE}")
    debug(f"tmp_dir       = {TMP_DIR}")
    if RETENTION_ENABLED:
        debug(f"tmp_dir_retention = {TMP_DIR_RETENTION}")
        debug(f"retention_source  = {RETENTION_SOURCE}")
    if DB_ENABLED:
        debug(f"db_container      = {DB_CONTAINER or '(direct, no Docker)'}")
        debug(f"db_host           = {DB_HOST or '(default socket)'}")
        debug(f"db_credentials    = {DB_CREDENTIALS_FILE}")
        debug(f"dump_dir          = {DUMP_DIR}")
        debug(f"db_list           = {DB_LIST}")
        debug(f"mysqldump_options = {MYSQLDUMP_OPTIONS or '(defaults only)'}")
        debug(f"dump_timeout     = {DUMP_TIMEOUT}")
        debug(f"min_free_space   = {MIN_FREE_SPACE_MB} MB")
    if ENCRYPT_ENABLED:
        debug(f"encrypt_passphrase_file = {ENCRYPT_PASSPHRASE_FILE}")
        debug(f"encrypt_paths     = {ENCRYPT_PATHS}")
        debug(f"encrypt_databases = {ENCRYPT_DATABASES}")
    debug(f"backup_paths  = {BACKUP_PATHS}")
    debug(f"max_retries   = {S3_MAX_RETRIES}")
    debug(f"smtp_host     = {SMTP_HOST}")
    debug(f"mail_to       = {MAIL_TO}")
    debug(f"http_proxy    = {os.environ['HTTP_PROXY']}")
# ─── Pasos del backup: configs ───────────────────────────────────────────────

def copy_retention():
    """
    Copia retention.dat desde /var/lib/naemon/ al directorio temporal
    usando sudo, con verificación de errores via subprocess.
    Solo se ejecuta si [Retention] enabled = true en el conf.
    """
    dest_dir = TMP_DIR_RETENTION
    dest_file = os.path.join(dest_dir, os.path.basename(RETENTION_SOURCE))

    debug(f"copy_retention: {RETENTION_SOURCE} → {dest_dir}/")

    try:
        os.makedirs(dest_dir, exist_ok=True)
    except Exception as e:
        log(f"ERROR: Falló al crear directorio {dest_dir}: {e}")
        return False

    commands = [
        ["sudo", "cp", RETENTION_SOURCE, dest_dir + "/"],
        ["sudo", "chown", f"{RETENTION_OWNER}:{RETENTION_GROUP}", dest_file],
        ["sudo", "chmod", RETENTION_PERMS, dest_file],
    ]

    for cmd in commands:
        debug(f"Ejecutando: {' '.join(cmd)}")
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            detail = e.stderr.strip() if e.stderr else f"Exit code: {e.returncode}"
            log(f"ERROR: Falló copy_retention — '{' '.join(cmd)}': {detail}")
            return False
        except Exception as e:
            log(f"ERROR: Falló copy_retention — '{' '.join(cmd)}': {e}")
            return False

    debug("copy_retention: OK")
    return True
def create_archive():
    """
    Crea un .tar.gz con todas las rutas configuradas.
    Si retention está habilitado, incluye retention.dat del directorio temporal.
    Las rutas inexistentes se omiten con un WARNING en el log.
    Cada ruta agregada se loguea individualmente.
    """
    archive_path = os.path.join(TMP_DIR, ARCHIVE_NAME)

    debug(f"Creando archive: {archive_path}")

    try:
        os.makedirs(TMP_DIR, exist_ok=True)
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.dereference = DEREFERENCE_SYMLINKS
            for path in BACKUP_PATHS:
                if os.path.exists(path):
                    tar.add(path, arcname=os.path.relpath(path, "/"))
                    log(f"Agregado al archive: {path}")
                else:
                    log(f"WARNING: Ruta no encontrada, se omite: {path}")

            # Auto-incluir retention.dat si está habilitado
            if RETENTION_ENABLED:
                retention_dest = os.path.join(
                    TMP_DIR_RETENTION, os.path.basename(RETENTION_SOURCE)
                )
                if os.path.exists(retention_dest):
                    tar.add(retention_dest,
                            arcname=os.path.relpath(retention_dest, "/"))
                    log(f"Agregado al archive: {retention_dest}")
                else:
                    log(f"WARNING: retention.dat no encontrado en destino temporal: "
                        f"{retention_dest}")

        size_bytes = os.path.getsize(archive_path)
        size_mb = size_bytes / (1024 * 1024)
        log(f"Archivo creado: {ARCHIVE_NAME} ({size_mb:.1f} MB)")
        return True

    except Exception as e:
        log(f"ERROR: Falló al crear el archivo tar: {e}")
        return False
# ─── Pasos del backup: S3 upload + verificación ──────────────────────────────

def upload_and_verify(local_path, s3_key):
    """
    Sube un archivo a S3 y verifica con head_object + size + MD5 checksum.
    Retorna True si OK, False si falla.
    """
    debug(f"Conectando a S3 (region={AWS_REGION}, max_retries={S3_MAX_RETRIES})")

    s3_config = BotoConfig(
        retries={'max_attempts': S3_MAX_RETRIES, 'mode': 'standard'}
    )
    s3 = boto3.client("s3", region_name=AWS_REGION, config=s3_config)

    try:
        debug(f"Subiendo {local_path} → s3://{BUCKET_NAME}/{s3_key}")
        s3.upload_file(local_path, BUCKET_NAME, s3_key)

        # Verificación: head_object + size + checksum
        debug("Verificando upload con head_object...")
        response = s3.head_object(Bucket=BUCKET_NAME, Key=s3_key)
        remote_size = response['ContentLength']
        remote_etag = response['ETag'].strip('"')
        local_size = os.path.getsize(local_path)

        # Size check
        if remote_size != local_size:
            log(f"ERROR: Verificación fallida — tamaño S3 ({remote_size}) "
                f"≠ local ({local_size})")
            return False

        # MD5 check (solo si no es multipart upload)
        if '-' not in remote_etag:
            local_md5 = calculate_md5(local_path)
            if local_md5 != remote_etag:
                log(f"ERROR: Verificación fallida — checksum S3 ({remote_etag}) "
                    f"≠ local ({local_md5})")
                return False
            debug(f"Verificación OK: tamaño ({remote_size}) + checksum ({remote_etag})")
        else:
            debug(f"Verificación OK: tamaño ({remote_size}) [multipart, checksum omitido]")

        log(f"Backup subido y verificado en S3: {s3_key} ({remote_size} bytes)")
        return True

    except Exception as e:
        log(f"ERROR: Falló la subida a S3: {e}")
        return False
# ─── Pasos del backup: databases ──────────────────────────────────────────────

def preflight_checks():
    """
    Verifica que todo esté en orden antes de intentar los dumps.
    Retorna True si OK, False si algo falla.
    """
    debug("Pre-flight: iniciando verificaciones...")

    # Verificar archivo de credenciales
    if not os.path.exists(DB_CREDENTIALS_FILE):
        log(f"ERROR: Pre-flight fallido — archivo de credenciales no encontrado: {DB_CREDENTIALS_FILE}")
        return False
    debug(f"Pre-flight: archivo de credenciales OK")

    # Verificar Docker o mysqldump
    if DB_CONTAINER:
        # Modo Docker
        debug(f"Pre-flight: verificando Docker...")
        try:
            result = subprocess.run(["which", "docker"], capture_output=True, text=True)
            if result.returncode != 0:
                log("ERROR: Pre-flight fallido — 'docker' no encontrado en PATH")
                return False
            debug("Pre-flight: Docker disponible")

            # Verificar que el container está corriendo
            debug(f"Pre-flight: verificando container '{DB_CONTAINER}'...")
            inspect_cmd = ["docker", "inspect", "--format",
                           "{{.State.Running}}", DB_CONTAINER]
            result = subprocess.run(inspect_cmd, capture_output=True, text=True)
            if result.returncode != 0:
                detail = result.stderr.strip() if result.stderr else f"Exit code: {result.returncode}"
                log(f"ERROR: Pre-flight fallido — no se puede inspeccionar container '{DB_CONTAINER}': {detail}")
                return False
            if result.stdout.strip() != "true":
                log(f"ERROR: Pre-flight fallido — container '{DB_CONTAINER}' no está corriendo")
                return False
            debug(f"Pre-flight: container '{DB_CONTAINER}' corriendo")

        except Exception as e:
            log(f"ERROR: Pre-flight fallido — {e}")
            return False
    else:
        # Modo directo
        debug("Pre-flight: verificando mysqldump...")
        try:
            result = subprocess.run(["which", "mysqldump"], capture_output=True, text=True)
            if result.returncode != 0:
                log("ERROR: Pre-flight fallido — 'mysqldump' no encontrado en PATH")
                return False
            debug("Pre-flight: mysqldump disponible")
        except Exception as e:
            log(f"ERROR: Pre-flight fallido — {e}")
            return False

    # Verificar credenciales para cada DB
    cred_cfg = configparser.RawConfigParser()
    cred_cfg.read(DB_CREDENTIALS_FILE)

    for dbname in DB_LIST:
        if not cred_cfg.has_section(dbname):
            log(f"ERROR: Pre-flight fallido — no hay credenciales para '{dbname}' en {DB_CREDENTIALS_FILE}")
            return False
        user = cred_cfg.get(dbname, 'user', fallback='').strip()
        if not user:
            log(f"ERROR: Pre-flight fallido — 'user' vacío para '{dbname}' en {DB_CREDENTIALS_FILE}")
            return False
        debug(f"Pre-flight: credenciales OK para '{dbname}'")

    # Verificar espacio en disco
    debug("Verificando espacio en disco...")
    try:
        os.makedirs(DUMP_DIR, exist_ok=True)
        free_bytes = shutil.disk_usage(DUMP_DIR).free
        free_mb = free_bytes / (1024 * 1024)
        if free_mb < MIN_FREE_SPACE_MB:
            log(f"ERROR: Espacio insuficiente en {DUMP_DIR} ({free_mb:.0f} MB libres, mínimo {MIN_FREE_SPACE_MB} MB)")
            return False
        debug(f"Espacio en disco OK: {free_mb:.0f} MB libres (mínimo {MIN_FREE_SPACE_MB} MB)")
    except Exception as e:
        log(f"ERROR: Pre-flight fallido — no se pudo verificar espacio en disco: {e}")
        return False

    debug("Pre-flight OK: todas las verificaciones pasaron")
    return True
def dump_single_db(dbname):
    """
    Ejecuta mysqldump para una DB, comprime, sube a S3 y verifica.
    Retorna True si OK, False si falla.
    """
    # Cargar credenciales de esta DB
    cred_cfg = configparser.RawConfigParser()
    cred_cfg.read(DB_CREDENTIALS_FILE)
    db_user = cred_cfg.get(dbname, 'user')
    db_pass = cred_cfg.get(dbname, 'password')

    # Construir comando mysqldump
    extra_opts = MYSQLDUMP_OPTIONS.split() if MYSQLDUMP_OPTIONS else []
    base_opts = MYSQLDUMP_DEFAULTS.split()

    if DB_CONTAINER:
        cmd = ["docker", "exec", DB_CONTAINER, "mysqldump"] + base_opts + extra_opts + \
              ["-u", db_user, f"-p{db_pass}"]
        if DB_HOST:
            cmd += ["-h", DB_HOST]
        cmd.append(dbname)
    else:
        cmd = ["mysqldump"] + base_opts + extra_opts + \
              ["-u", db_user, f"-p{db_pass}"]
        if DB_HOST:
            cmd += ["-h", DB_HOST]
        cmd.append(dbname)

    sql_path = os.path.join(DUMP_DIR, f"{dbname}.sql")
    debug(f"Ejecutando mysqldump para '{dbname}'...")

    # Ejecutar mysqldump con timeout, stdout a archivo
    try:
        with open(sql_path, 'wb') as f:
            result = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE,
                                    timeout=DUMP_TIMEOUT, check=True)
    except subprocess.TimeoutExpired:
        log(f"ERROR: Dump de '{dbname}' falló — timeout después de {DUMP_TIMEOUT}s")
        # Limpiar .sql parcial
        if os.path.exists(sql_path):
            os.remove(sql_path)
        return False
    except subprocess.CalledProcessError as e:
        detail = e.stderr.strip() if e.stderr else f"Exit code: {e.returncode}"
        log(f"ERROR: Dump de '{dbname}' falló — mysqldump: {detail}")
        if os.path.exists(sql_path):
            os.remove(sql_path)
        return False
    except Exception as e:
        log(f"ERROR: Dump de '{dbname}' falló — {e}")
        if os.path.exists(sql_path):
            os.remove(sql_path)
        return False

    # Validar que el dump no esté vacío
    if os.path.getsize(sql_path) == 0:
        log(f"ERROR: Dump de '{dbname}' está vacío")
        os.remove(sql_path)
        return False

    # Validar header del dump
    try:
        with open(sql_path, 'rb') as f:
            header = f.readline()
        if not (header.startswith(b'-- MySQL dump') or header.startswith(b'-- MariaDB dump')):
            log(f"ERROR: Dump de '{dbname}' no es un mysqldump válido (header: {header[:50]})")
            os.remove(sql_path)
            return False
    except Exception as e:
        log(f"ERROR: Dump de '{dbname}' falló al validar header: {e}")
        if os.path.exists(sql_path):
            os.remove(sql_path)
        return False

    # Log del dump creado
    sql_size = os.path.getsize(sql_path)
    sql_mb = sql_size / (1024 * 1024)
    log(f"Dump creado: {dbname} ({sql_mb:.1f} MB)")

    # Comprimir el .sql en su propio .tar.gz
    db_tar_name = f"{HOSTNAME}_{DATE}_{dbname}.tar.gz"
    db_tar_path = os.path.join(DUMP_DIR, db_tar_name)

    try:
        with tarfile.open(db_tar_path, "w:gz") as tar:
            tar.add(sql_path, arcname=f"{dbname}.sql")
        log(f"Agregado al archive: {dbname}.sql")
    except Exception as e:
        log(f"ERROR: Falló al comprimir dump de '{dbname}': {e}")
        if os.path.exists(sql_path):
            os.remove(sql_path)
        if os.path.exists(db_tar_path):
            os.remove(db_tar_path)
        return False

    # Borrar .sql inmediatamente para liberar espacio
    os.remove(sql_path)
    debug(f"SQL temporal eliminado: {dbname}.sql")

    # ¿Encriptar esta DB?
    should_encrypt = ENCRYPT_ENABLED and dbname in ENCRYPT_DATABASES

    if should_encrypt:
        debug(f"DB '{dbname}' marcada para encriptación")
        enc_name = db_tar_name + ".enc"
        enc_path = os.path.join(DUMP_DIR, enc_name)

        if not encrypt_file(db_tar_path, enc_path):
            if os.path.exists(db_tar_path):
                os.remove(db_tar_path)
            return False

        # Borrar .tar.gz plano inmediatamente
        os.remove(db_tar_path)
        debug(f"Archive plano eliminado: {db_tar_name}")

        # Subir .enc a S3
        s3_key = f"{S3_PREFIX}/databases/{enc_name}"
        if not upload_and_verify(enc_path, s3_key):
            log(f"ERROR: Falló la subida del dump encriptado de '{dbname}' a S3")
            if os.path.exists(enc_path):
                os.remove(enc_path)
            return False

        if os.path.exists(enc_path):
            os.remove(enc_path)
        debug(f"Archive encriptado temporal eliminado: {enc_name}")
    else:
        # Subir .tar.gz plano a S3
        s3_key = f"{S3_PREFIX}/databases/{db_tar_name}"
        if not upload_and_verify(db_tar_path, s3_key):
            log(f"ERROR: Falló la subida del dump de '{dbname}' a S3")
            if os.path.exists(db_tar_path):
                os.remove(db_tar_path)
            return False

        # Limpiar .tar.gz local
        os.remove(db_tar_path)
        debug(f"Tar.gz temporal eliminado: {db_tar_name}")

    return True
def dump_databases():
    """
    Ejecuta el backup de todas las bases de datos configuradas.
    Retorna (ok_count, fail_count).
    """
    total = len(DB_LIST)
    log(f"=== INICIANDO BACKUP DE DATABASES ({total} bases) ===")

    # Pre-flight checks
    if not preflight_checks():
        log(f"=== BACKUP DE DATABASES: 0/{total} OK, {total} fallidos ===")
        return 0, total

    ok_count = 0
    fail_count = 0

    for dbname in DB_LIST:
        debug(f"Procesando DB: {dbname}")
        if dump_single_db(dbname):
            ok_count += 1
        else:
            fail_count += 1

    if fail_count == 0:
        log(f"=== BACKUP DE DATABASES: {ok_count}/{total} OK ===")
    else:
        log(f"=== BACKUP DE DATABASES: {ok_count}/{total} OK, {fail_count} fallido(s) ===")

    return ok_count, fail_count
# ─── Encriptación ───────────────────────────────────────────────────────────

def preflight_encrypt():
    """
    Verifica que todo esté en orden para la encriptación.
    Carga la passphrase del archivo de credenciales.
    Retorna True si OK, False si falla.
    """
    debug("Pre-flight encriptación: iniciando verificaciones...")

    # Verificar que openssl existe
    try:
        result = subprocess.run(["which", "openssl"], capture_output=True, text=True)
        if result.returncode != 0:
            log("ERROR: Pre-flight encriptación fallido — 'openssl' no encontrado en PATH")
            return False
        debug("Pre-flight encriptación: openssl disponible")
    except Exception as e:
        log(f"ERROR: Pre-flight encriptación fallido — {e}")
        return False

    # Verificar que el archivo de passphrase existe
    if not os.path.exists(ENCRYPT_PASSPHRASE_FILE):
        log(f"ERROR: Pre-flight encriptación fallido — archivo de passphrase no encontrado: {ENCRYPT_PASSPHRASE_FILE}")
        return False
    debug(f"Pre-flight encriptación: archivo de passphrase OK")

    # Cargar la passphrase del archivo de credenciales
    cred_cfg = configparser.RawConfigParser()
    cred_cfg.read(ENCRYPT_PASSPHRASE_FILE)

    if not cred_cfg.has_section('Encryption'):
        log(f"ERROR: Pre-flight encriptación fallido — sección [Encryption] no encontrada en {ENCRYPT_PASSPHRASE_FILE}")
        return False

    global ENCRYPT_PASSPHRASE
    ENCRYPT_PASSPHRASE = cred_cfg.get('Encryption', 'passphrase', fallback='').strip()
    if not ENCRYPT_PASSPHRASE:
        log(f"ERROR: Pre-flight encriptación fallido — 'passphrase' vacía en [Encryption] de {ENCRYPT_PASSPHRASE_FILE}")
        return False
    debug("Pre-flight encriptación: passphrase cargada OK")

    debug("Pre-flight encriptación OK: todas las verificaciones pasaron")
    return True
def encrypt_file(input_path, output_path):
    """
    Encripta un archivo con OpenSSL AES-256-CBC + PBKDF2.
    La passphrase se pasa por stdin (no toca disco ni aparece en ps).
    Retorna True si OK, False si falla.
    """
    enc_name = os.path.basename(output_path)
    debug(f"Encriptando {os.path.basename(input_path)} → {enc_name}...")

    try:
        subprocess.run(
            ["openssl", "enc", "-aes-256-cbc", "-salt", "-pbkdf2",
             "-in", input_path, "-out", output_path, "-pass", "stdin"],
            input=ENCRYPT_PASSPHRASE.encode() + b"\n",
            capture_output=True, check=True
        )
    except subprocess.CalledProcessError as e:
        detail = e.stderr.strip() if e.stderr else f"Exit code: {e.returncode}"
        log(f"ERROR: Falló encriptación de {enc_name}: {detail}")
        return False
    except Exception as e:
        log(f"ERROR: Falló encriptación de {enc_name}: {e}")
        return False

    # Verificar que el .enc no está vacío
    if os.path.getsize(output_path) == 0:
        log(f"ERROR: Encriptación fallida — {enc_name} está vacío")
        return False

    log(f"Archive encriptado: {enc_name}")
    return True
def create_secure_archive():
    """
    Crea un .tar.gz separado con los paths sensibles de [Encrypt_Paths].
    Retorna el path del .tar.gz (sin encriptar) o None si falla.
    """
    secure_archive_name = f"{HOSTNAME}_{DATE}_secure.tar.gz"
    secure_archive_path = os.path.join(TMP_DIR, secure_archive_name)

    debug(f"Creando archive seguro: {secure_archive_path}")

    try:
        os.makedirs(TMP_DIR, exist_ok=True)
        with tarfile.open(secure_archive_path, "w:gz") as tar:
            tar.dereference = DEREFERENCE_SYMLINKS
            for path in ENCRYPT_PATHS:
                if os.path.exists(path):
                    tar.add(path, arcname=os.path.relpath(path, "/"))
                    log(f"Agregado al archive: {path}")
                else:
                    log(f"WARNING: Ruta no encontrada, se omite: {path}")

        size_bytes = os.path.getsize(secure_archive_path)
        size_mb = size_bytes / (1024 * 1024)
        log(f"Archivo seguro creado: {secure_archive_name} ({size_mb:.1f} MB)")
        return secure_archive_path

    except Exception as e:
        log(f"ERROR: Falló al crear el archive seguro: {e}")
        return None
# ─── Email ───────────────────────────────────────────────────────────────────

def send_mail():
    """Envía el contenido del log por email. Falla con WARNING, no afecta el backup."""
    debug(f"Enviando email a {MAIL_TO} via {SMTP_HOST}...")
    try:
        with open(LOG_FILE, 'r') as f:
            contenido = f.read()

        msg = MIMEText(contenido)
        msg['Subject'] = f"Backup {SERVICE_NAME} {HOSTNAME} | {DATE}"
        msg['From'] = MAIL_FROM
        msg['To'] = MAIL_TO

        smtplib.SMTP(SMTP_HOST, timeout=SMTP_TIMEOUT).send_message(msg)
        debug("Email enviado correctamente")
        return True

    except Exception as e:
        log(f"WARNING: Falló el envío de e-mail: {e}")
        return False
# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    global DEBUG_MODE

    parser = argparse.ArgumentParser(
        description="Backup de configuraciones y bases de datos a S3"
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_FILE,
        help=f"Path al archivo .conf (default: {DEFAULT_CONFIG_FILE})"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=False,
        help="Modo debug: imprime a stdout el progreso detallado"
    )
    args = parser.parse_args()

    DEBUG_MODE = args.debug

    load_config(args.config)

    # Limpiar leftovers de una corrida anterior que pudo haber sido interrumpida
    debug("Limpiando leftovers de corrida anterior...")
    cleanup_temp()

    exit_code = 1  # default: failure (se sobreescribe solo si todo OK)
    backup_failed = False

    try:
        # Truncar log para esta corrida
        debug(f"Truncando log: {LOG_FILE}")
        # Crear directorio del log si no existe
        log_dir = os.path.dirname(LOG_FILE)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)
            debug(f"Directorio creado: {log_dir}")
        with open(LOG_FILE, 'w') as f:
            f.write("")

        log(f"=== INICIANDO BACKUP DE {SERVICE_NAME} ({HOSTNAME}) ===")

        # ── Pre-flight encriptación ──────────────────────────────────────
        if ENCRYPT_ENABLED:
            debug("Encriptación habilitada — ejecutando pre-flight encriptación")
            if not preflight_encrypt():
                backup_failed = True

        # ── Paso 1: Configs ────────────────────────────────────────────────
        if RETENTION_ENABLED:
            debug("Retention habilitado — ejecutando copy_retention()")
            if not copy_retention():
                backup_failed = True
        else:
            debug("Retention deshabilitado — se saltea copy_retention()")

        if not backup_failed:
            if BACKUP_PATHS or RETENTION_ENABLED:
                if not create_archive():
                    backup_failed = True
                else:
                    config_key = f"{S3_PREFIX}/{ARCHIVE_NAME}"
                    if not upload_and_verify(os.path.join(TMP_DIR, ARCHIVE_NAME), config_key):
                        backup_failed = True
            else:
                debug("Sin [Backup_Paths] y sin retention — se saltea backup de configs")

        # ── Paso 1b: Configs encriptados ──────────────────────────────────
        if not backup_failed and ENCRYPT_ENABLED and ENCRYPT_PATHS:
            debug("Encriptación de configs habilitada — creando archive seguro")
            secure_tar = create_secure_archive()
            if secure_tar is None:
                backup_failed = True
            else:
                secure_tar_name = os.path.basename(secure_tar)
                enc_name = secure_tar_name + ".enc"
                enc_path = os.path.join(TMP_DIR, enc_name)

                if not encrypt_file(secure_tar, enc_path):
                    backup_failed = True
                else:
                    # Borrar .tar.gz plano inmediatamente
                    os.remove(secure_tar)
                    debug(f"Archive plano eliminado: {secure_tar_name}")

                    secure_key = f"{S3_PREFIX}/{enc_name}"
                    if not upload_and_verify(enc_path, secure_key):
                        backup_failed = True
                    if os.path.exists(enc_path):
                        os.remove(enc_path)

        # ── Paso 2: Databases ──────────────────────────────────────────────
        if DB_ENABLED:
            debug("Database backup habilitado — ejecutando dump_databases()")
            ok_count, fail_count = dump_databases()
            if fail_count > 0:
                backup_failed = True
        else:
            debug("Database backup deshabilitado — se saltea dump_databases()")

        # ── Resultado final ─────────────────────────────────────────────────
        if not backup_failed:
            log("Limpiando archivos temporales")
            log(SUCCESS_MARKER)
            exit_code = 0

        send_mail()  # WARNING en caso de fallo, no afecta el estado del backup

        return exit_code

    finally:
        debug("Ejecutando cleanup final (try/finally)...")
        cleanup_temp()

        # Mensaje de cierre (solo debug)
        if exit_code != 0:
            debug("=== BACKUP FINALIZADO CON ERRORES ===")
        elif HAD_WARNINGS:
            debug("=== BACKUP FINALIZADO CON WARNINGS ===")
        else:
            debug("=== BACKUP FINALIZADO SIN ERRORES ===")
if __name__ == "__main__":
    sys.exit(main())
