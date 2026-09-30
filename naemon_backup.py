#!/usr/bin/env python3
"""
naemon_backup.py — Backup de configuraciones a S3

Dependencias:
- Python 3.8+
- boto3          (pip install boto3 o apt install python3-boto3)
- botocore       (incluido con boto3)
- Acceso a AWS   (IAM Role en EC2 o ~/.aws/credentials)
- sudo NOPASSWD  (para copiar retention.dat de /var/lib/naemon/ si está habilitado)

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

    debug(f"Cargando configuración desde: {config_file}")

    if not os.path.exists(config_file):
        print(f"ERROR: Archivo de configuración no encontrado: {config_file}")
        sys.exit(1)

    cfg = configparser.ConfigParser()
    cfg.read(config_file)

    # ── Determinar si retention está habilitado (default: false) ─────────────
    RETENTION_ENABLED = cfg.getboolean('Retention', 'enabled', fallback=False)
    debug(f"Retention enabled = {RETENTION_ENABLED}")

    # ── Construir lista de valores obligatorios dinámicamente ───────────────
    required = {
        'General':    ['service_name'],  # dereference_symlinks tiene default true
        'AWS':        ['bucket_name', 's3_prefix', 'region'],
        'Proxy':      ['http_proxy', 'https_proxy'],
        'Mail':       ['smtp_host', 'from', 'to'],
    }

    # Paths: tmp_dir_retention solo es obligatorio si retention está activo
    if RETENTION_ENABLED:
        required['Paths'] = ['log_file', 'tmp_dir', 'tmp_dir_retention']
        required['Retention'] = ['source', 'owner', 'group', 'permissions']
    else:
        required['Paths'] = ['log_file', 'tmp_dir']

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

    # Backup_Paths — valor multilinea, una ruta por línea
    raw = cfg.get('Backup_Paths', 'path', fallback='')
    BACKUP_PATHS = [os.path.expanduser(p.strip())
                    for p in raw.strip().splitlines() if p.strip()]
    if not BACKUP_PATHS:
        print("ERROR: No se han configurado rutas de backup en [Backup_Paths]")
        sys.exit(1)

    SMTP_HOST = cfg.get('Mail', 'smtp_host')
    MAIL_FROM = cfg.get('Mail', 'from')
    MAIL_TO = cfg.get('Mail', 'to')

    # Proxy — obligatorio (sin proxy no hay conexión a AWS)
    os.environ['HTTP_PROXY'] = cfg.get('Proxy', 'http_proxy')
    os.environ['HTTPS_PROXY'] = cfg.get('Proxy', 'https_proxy')

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
    debug(f"backup_paths  = {BACKUP_PATHS}")
    debug(f"max_retries   = {S3_MAX_RETRIES}")
    debug(f"smtp_host     = {SMTP_HOST}")
    debug(f"mail_to       = {MAIL_TO}")
    debug(f"http_proxy    = {os.environ['HTTP_PROXY']}")


# ─── Pasos del backup ───────────────────────────────────────────────────────

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
    archive_name = f"{HOSTNAME}_{DATE}.tar.gz"
    archive_path = os.path.join(TMP_DIR, archive_name)

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
        log(f"Archivo creado: {archive_name} ({size_mb:.1f} MB)")
        return True

    except Exception as e:
        log(f"ERROR: Falló al crear el archivo tar: {e}")
        return False


def upload_to_s3():
    """
    Sube el archivo a S3 con retries configurables y verifica la subida
    con head_object comparando el tamaño.
    """
    archive_name = f"{HOSTNAME}_{DATE}.tar.gz"
    archive_path = os.path.join(TMP_DIR, archive_name)
    key = f"{S3_PREFIX}/{archive_name}"

    debug(f"Conectando a S3 (region={AWS_REGION}, max_retries={S3_MAX_RETRIES})")

    s3_config = BotoConfig(
        retries={'max_attempts': S3_MAX_RETRIES, 'mode': 'standard'}
    )
    s3 = boto3.client("s3", region_name=AWS_REGION, config=s3_config)

    try:
        debug(f"Subiendo {archive_path} → s3://{BUCKET_NAME}/{key}")
        s3.upload_file(archive_path, BUCKET_NAME, key)

        # Verificación: head_object + comparación de tamaño
        debug(f"Verificando upload con head_object...")
        response = s3.head_object(Bucket=BUCKET_NAME, Key=key)
        remote_size = response['ContentLength']
        local_size = os.path.getsize(archive_path)

        if remote_size != local_size:
            log(f"ERROR: Verificación fallida — tamaño S3 ({remote_size}) "
                f"≠ local ({local_size})")
            return False

        debug(f"Verificación OK: tamaño S3 ({remote_size}) = local ({local_size})")
        log(f"Backup subido y verificado en S3: {key} ({remote_size} bytes)")
        return True

    except Exception as e:
        log(f"ERROR: Falló la subida a S3: {e}")
        return False


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
        description="Backup de configuraciones a S3"
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

    try:
        # Truncar log para esta corrida
        debug(f"Truncando log: {LOG_FILE}")
        with open(LOG_FILE, 'w') as f:
            f.write("")

        log(f"=== INICIANDO BACKUP DE {SERVICE_NAME} ({HOSTNAME}) ===")

        if RETENTION_ENABLED:
            debug("Retention habilitado — ejecutando copy_retention()")
            if not copy_retention():
                return 1
        else:
            debug("Retention deshabilitado — se saltea copy_retention()")

        if not create_archive():
            return 1
        if not upload_to_s3():
            return 1

        log("Limpiando archivos temporales")
        log(SUCCESS_MARKER)

        send_mail()  # WARNING en caso de fallo, no afecta el estado del backup

        exit_code = 0
        return 0

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
