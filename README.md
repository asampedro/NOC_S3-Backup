# S3 Backup — Configuration Backup to AWS S3

Script de backup que comprime configuraciones en un archivo `.tar.gz`, lo sube a un bucket de S3, envía el resultado por email y mantiene un log local. Incluye un plugin de monitoreo para Naemon/Nagios que verifica el estado del backup a partir del log.

Diseñado para ser reutilizable en distintos tipos de hosts (Naemon, Grafana, servidores NOC, etc.) mediante un archivo de configuración `.conf` por host.

## Archivos del proyecto

| Archivo | En .gitignore | Descripción |
|---|---|---|
| `naemon_backup.py` | No | Script principal de backup |
| `check_s3_backup.py` | No | Plugin de monitoreo Naemon/Nagios |
| `naemon_config_example.cfg` | No | Configuración de ejemplo para Naemon (command + service) |
| `s3bkp.conf` | **Sí** | Configuración con valores reales del entorno |
| `s3bkp.conf.example` | No | Plantilla de configuración sin valores sensibles |
| `S3backup.log` | Sí | Log de la última ejecución (truncado en cada corrida) |
| `README.md` | No | Esta documentación |
| `CONTEXT.md` | No | Contexto del proyecto para sesiones de Toqan |

## Dependencias

- **Python 3.8+**
- **boto3** — `pip install boto3` o `apt install python3-boto3`
- **botocore** — incluido con boto3
- **Acceso a AWS** — IAM Role en EC2 o `~/.aws/credentials`
- **sudo NOPASSWD** — el usuario que ejecuta el script necesita sudo sin contraseña para copiar `retention.dat` (solo si `[Retention] enabled = true`)

## Instalación

### 1. Copiar los archivos al servidor

```bash
# Directorio del script (debe coincidir con el crontab)
mkdir -p /home/noc_user/S3backup
cp naemon_backup.py /home/noc_user/S3backup/
cp s3bkp.conf.example /home/noc_user/S3backup/s3bkp.conf
```

### 1b. Crear el directorio de log

El log debe estar en un path donde tanto el usuario que corre el script como Naemon puedan acceder:

```bash
sudo mkdir -p /var/log/naemon/s3backup
sudo chown noc_user:naemon /var/log/naemon/s3backup
sudo chmod 775 /var/log/naemon/s3backup
```

### 2. Editar la configuración

```bash
vi /home/noc_user/S3backup/s3bkp.conf
```

Ajustar todos los valores según el entorno. Ver la sección [Configuración](#configuración) más abajo.

### 3. Configurar el crontab

```bash
crontab -e
```

```cron
# EJECUCION DE BACKUP
00 22 * * * python3 /home/noc_user/S3backup/naemon_backup.py
```

### 4. Instalar el plugin de monitoreo

```bash
sudo cp check_s3_backup.py /usr/lib/naemon/plugins/noc/
sudo chmod +x /usr/lib/naemon/plugins/noc/check_s3_backup.py
```

### 5. Configurar el servicio en Naemon

```bash
sudo cp naemon_config_example.cfg /etc/naemon/conf.d/noc/s3_backup.cfg
# Editar host_name, contacts y el path del log según el entorno
sudo vi /etc/naemon/conf.d/noc/s3_backup.cfg
sudo naemon -v /etc/naemon/naemon.cfg
sudo systemctl reload naemon
```

### 6. Monitoreo remoto via NRPE (opcional)

Si el backup corre en un host distinto al servidor Naemon, usar NRPE para ejecutar el check remotamente.

**En el host remoto (donde corre el backup):**

```bash
# Instalar NRPE
sudo apt install nagios-nrpe-server

# Instalar el plugin
sudo cp check_s3_backup.py /usr/lib/naemon/plugins/noc/
sudo chmod +x /usr/lib/naemon/plugins/noc/check_s3_backup.py

# Configurar el command en NRPE
# Agregar a /etc/nagios/nrpe.cfg o un archivo en /etc/nagios/nrpe.d/:
# command[check_s3_backup]=/usr/lib/naemon/plugins/noc/check_s3_backup.py -f /var/log/naemon/s3backup/S3backup.log

# Permitir al servidor Naemon
# Editar allowed_hosts en la config de NRPE:
# allowed_hosts=127.0.0.1,<NAEMON_SERVER_IP>

# Reiniciar NRPE
sudo systemctl restart nagios-nrpe-server
```

**En el servidor Naemon:**

```bash
# Probar la conexión NRPE
/usr/lib/naemon/plugins/check_nrpe -H <REMOTE_HOST_IP> -c check_s3_backup
```

Ver `naemon_config_example.cfg` para la definición del command y service NRPE.

## Configuración (`s3bkp.conf`)

El archivo de configuración usa formato INI con `configparser`. Debe estar en el mismo directorio que `naemon_backup.py`. Todas las secciones son obligatorias excepto `[Retention]` y `[S3]`.

### Secciones

#### `[General]` — Obligatoria
| Clave | Default | Descripción |
|---|---|---|
| `service_name` | — | Nombre del servicio que se respalda. Aparece en el log y el asunto del email. Ej: `Naemon`, `Grafana`, `NOC Server`. |
| `dereference_symlinks` | `true` | `true` = copia el archivo destino del symlink (recomendado para restore). `false` = preserva el symlink tal cual (solo funciona si se restaura en el mismo path). |

#### `[AWS]` — Obligatoria
| Clave | Descripción |
|---|---|
| `bucket_name` | Nombre del bucket S3 destino |
| `s3_prefix` | Prefijo (carpeta) dentro del bucket. Para separar por host, incluir el subfolder: `backups/naemon/Regional`, `backups/grafana`, etc. |
| `region` | Región de AWS donde está el bucket |

#### `[Proxy]` — Obligatoria
| Clave | Descripción |
|---|---|
| `http_proxy` | URL del proxy HTTP (sin proxy no hay conexión a AWS) |
| `https_proxy` | URL del proxy HTTPS |

#### `[Paths]` — Obligatoria
| Clave | Descripción |
|---|---|
| `log_file` | Path al archivo de log (soporta `~`). Debe estar en un path accesible por Naemon. Ej: `/var/log/naemon/s3backup/S3backup.log` |
| `tmp_dir` | Directorio temporal para el `.tar.gz` |
| `tmp_dir_retention` | Directorio temporal para `retention.dat`. **Solo obligatorio si `[Retention] enabled = true`.** Si retention está deshabilitado, se puede omitir. |

#### `[Retention]` — Opcional
| Clave | Default | Descripción |
|---|---|---|
| `enabled` | `false` | `true` = copia `retention.dat` y lo incluye en el archive (solo hosts con Naemon). `false` = saltea la rutina. Si la sección no existe, default = `false`. |
| `source` | — | Ruta origen de `retention.dat` (se copia con sudo). Solo obligatorio si `enabled = true`. |
| `owner` | — | Owner del archivo copiado. Solo obligatorio si `enabled = true`. |
| `group` | — | Group del archivo copiado. Solo obligatorio si `enabled = true`. |
| `permissions` | — | Permisos en formato octal (ej: `660`). Solo obligatorio si `enabled = true`. |

#### `[Backup_Paths]` — Obligatoria
| Clave | Descripción |
|---|---|
| `path` | Lista multilinea de rutas a incluir en el backup. Una por línea, indentadas. |

> `retention.dat` se incluye automáticamente desde `tmp_dir_retention` si `[Retention] enabled = true`. No es necesario listarlo en `[Backup_Paths]`.

#### `[Mail]` — Obligatoria
| Clave | Descripción |
|---|---|
| `smtp_host` | Servidor SMTP (normalmente `localhost`) |
| `from` | Remitente del email |
| `to` | Destinatario |

#### `[S3]` — Opcional
| Clave | Default | Descripción |
|---|---|---|
| `max_retries` | `5` | Máximo de intentos de subida (1 inicial + N retries) |

### Organización del bucket S3

El `s3_prefix` controla la ruta dentro del bucket. Para mantener los backups separados por host/tipo, configurar el prefix con subfolders:

| Host | `s3_prefix` | S3 key resultante |
|---|---|---|
| Naemon Regional | `backups/naemon/Regional` | `backups/naemon/Regional/host_YYYY-MM-DD.tar.gz` |
| Naemon Cross | `backups/naemon/Cross` | `backups/naemon/Cross/host_YYYY-MM-DD.tar.gz` |
| Naemon NR | `backups/naemon/NR` | `backups/naemon/NR/host_YYYY-MM-DD.tar.gz` |
| Grafana | `backups/grafana` | `backups/grafana/host_YYYY-MM-DD.tar.gz` |

## Cómo funciona el script (`naemon_backup.py`)

### Flujo de ejecución

1. **Carga y valida la configuración** desde `s3bkp.conf`. Si falta cualquier valor obligatorio, el script se detiene con error antes de hacer nada. La validación es condicional: los campos de `[Retention]` y `tmp_dir_retention` solo son obligatorios si `enabled = true`.
2. **Limpia directorios temporales** para remover leftovers de una corrida anterior interrumpida.
3. **Trunca el log** — el log siempre contiene solo la última corrida.
4. **Copia `retention.dat`** (si `enabled = true`) desde `/var/lib/naemon/` al directorio temporal usando `sudo` con `subprocess.run(check=True)`. Si falla, el script se detiene.
5. **Crea el archivo `.tar.gz`** con todas las rutas configuradas. Si `enabled = true`, también incluye `retention.dat` del directorio temporal. Las rutas inexistentes se omiten con un `WARNING` en el log. Registra el tamaño del archivo.
6. **Sube a S3** con retries configurables (backoff exponencial). Solo reintenta errores transitorios (timeouts, 5xx, throttling); los errores permanentes (AccessDenied, NoSuchBucket) no se reintentan.
7. **Verifica la subida** con `head_object` comparando el tamaño del objeto en S3 con el archivo local.
8. **Registra `=== BACKUP COMPLETADO EXITOSAMENTE ===`** en el log — este es el marcador que el plugin de monitoreo busca.
9. **Envía el email** con el contenido del log. Si el email falla, se registra como `WARNING:` (no afecta el estado del backup).
10. **Limpia los temporales** nuevamente (garantizado por `try/finally`, se ejecuta incluso si algo falla a mitad de la corrida).
11. **Exit code** — `0` si el backup fue exitoso, `1` si falló en cualquier paso.

### Log de backup

El log se trunca al inicio de cada corrida. Formato:

```
[2026-09-27 23:50:01] === INICIANDO BACKUP DE Naemon (naemon_server_1) ===
[2026-09-27 23:50:05] Agregado al archive: /etc/naemon
[2026-09-27 23:50:05] Agregado al archive: /etc/thruk/cgi.cfg
[2026-09-27 23:50:06] Agregado al archive: /usr/lib/nagios/plugins
[2026-09-27 23:50:06] Agregado al archive: /var/log/naemon
[2026-09-27 23:50:06] Agregado al archive: /home/noc_user/naemon_retention/retention.dat
[2026-09-27 23:50:07] Archivo creado: naemon_server_1_2026-09-27.tar.gz (18.2 MB)
[2026-09-27 23:50:10] Backup subido y verificado en S3: backups/naemon/Regional/naemon_server_1_2026-09-27.tar.gz (19084051 bytes)
[2026-09-27 23:50:10] Limpiando archivos temporales
[2026-09-27 23:50:10] === BACKUP COMPLETADO EXITOSAMENTE ===
```

### Prefijos de log

| Prefijo | Significado | ¿Afecta el monitoreo? |
|---|---|---|
| `=== INICIANDO BACKUP DE {service_name} ({host}) ===` | Inicio de la corrida | No |
| `Agregado al archive: ...` | Cada ruta incluida en el backup | No |
| `Archivo creado: ...` | Información del archivo (nombre + tamaño) | No |
| `Backup subido y verificado en S3: ...` | Subida exitosa | No |
| `=== BACKUP COMPLETADO EXITOSAMENTE ===` | **Marcador de éxito** — el plugin busca esta línea | **Sí — determina OK** |
| `ERROR:` | Falla del backup (retention, tar, S3) | **Sí — determina CRITICAL** |
| `WARNING:` | Falla no crítica (email, ruta no encontrada) | No — el backup sigue OK |

### Exit codes

| Código | Significado |
|---|---|
| `0` | Backup exitoso |
| `1` | Backup fallido (cualquier paso) |

## Cómo funciona el monitoreo (`check_s3_backup.py`)

### Lógica de estados

| Condición | Estado | Exit Code |
|---|---|---|
| Backup exitoso + timestamp < 48h | 🟢 OK | 0 |
| Backup exitoso + timestamp entre 48h y 72h | 🟡 WARNING | 1 |
| Backup fallido (cualquier edad) | 🔴 CRITICAL | 2 |
| Backup exitoso + timestamp > 72h | 🔴 CRITICAL | 2 |
| Log inexistente o vacío | 🔴 CRITICAL | 2 |
| No se puede leer el log | ⚪ UNKNOWN | 3 |

### Orden de evaluación

1. ¿Backup fallido? → **CRITICAL** (sin importar la antigüedad)
2. ¿Timestamp > 72h? → **CRITICAL** (aunque haya sido exitoso, está demasiado viejo)
3. ¿Timestamp > 48h? → **WARNING** (exitoso pero envejeciendo)
4. Todo OK → **OK**

### Modo debug (`--debug`)

El flag `--debug` imprime a stdout el progreso detallado del backup en tiempo real. Incluye:

- Valores de configuración cargados (service_name, bucket, proxy, paths, etc.)
- Cada comando `sudo` que se ejecuta en `copy_retention()`
- Cada paso del proceso (truncado de log, creación de archive, conexión a S3, verificación, email)
- Verificación de cleanup (confirmación de que los temporales se eliminaron)
- Mensaje de cierre con el resultado general del run
- Todo lo que va al log también se imprime en pantalla

Sin `--debug`, el script corre en silencio (ideal para cron).

```bash
# Modo normal (cron, silencioso)
python3 /home/noc_user/S3backup/naemon_backup.py

# Modo debug (manual, verbose)
python3 /home/noc_user/S3backup/naemon_backup.py --debug

# Modo debug con config custom
python3 /home/noc_user/S3backup/naemon_backup.py --debug --config /tmp/test.conf
```

#### Colores en modo debug

Los colores se aplican solo en la terminal (TTY detection). No aparecen en el log ni email.

| Color | Tipo | Ejemplos |
|---|---|---|
| **Bold red** | Error crítico | `ERROR: Falló copy_retention...` |
| **Bold green** | Fin exitoso del run | `=== BACKUP COMPLETADO EXITOSAMENTE ===` |
| **Bold cyan** | Inicio del run | `=== INICIANDO BACKUP DE Naemon (host) ===` |
| **Bold yellow** | Cierre con warnings | `=== BACKUP FINALIZADO CON WARNINGS ===` |
| Red | — | *(los errores siempre son bold red)* |
| Yellow | Warnings | `WARNING: Ruta no encontrada...` |
| Green | Éxito de paso | `Agregado al archive:`, `Archivo creado:`, `Cleanup OK:`, `copy_retention: OK`, `Verificación OK:` |
| Cyan | Acciones en progreso | `Cargando`, `Ejecutando:`, `Subiendo`, `Conectando a S3`, `Limpiando` |
| Default | Datos / variables | `service_name = Naemon`, `bucket_name = ...` |

#### Mensaje de cierre

Al final del run (en `--debug`), se imprime un mensaje de resumen:

| Mensaje | Color | Condición |
|---|---|---|
| `=== BACKUP FINALIZADO SIN ERRORES ===` | Bold green | Backup exitoso, sin warnings |
| `=== BACKUP FINALIZADO CON WARNINGS ===` | Bold yellow | Backup exitoso, pero hubo warnings (ruta faltante, email falló, cleanup falló) |
| `=== BACKUP FINALIZADO CON ERRORES ===` | Bold red | Backup fallido en algún paso |

#### Verificación de cleanup

El script verifica que los directorios temporales se eliminen correctamente:

- Si el cleanup es exitoso → `Cleanup OK: /tmp/naemon_backup` (solo en `--debug`)
- Si el cleanup falla → `WARNING: No se pudo eliminar /tmp/naemon_backup` (siempre en el log y email)

### Uso del plugin

```
usage: check_s3_backup.py [-h] [-f LOG_FILE] [-w WARNING] [-c CRITICAL]

  -f, --log-file    Path al log de backup (default: ~/backup/log/backup.log)
  -w, --warning     Threshold de warning en horas (default: 48)
  -c, --critical    Threshold de critical en horas (default: 72)
  -h, --help        Mostrar ayuda
```

El plugin extrae el hostname del log con un regex genérico `INICIANDO BACKUP DE .+ (hostname)` — funciona con cualquier `service_name`.

### Performance data

El plugin emite performance data compatible con PNP4Nagios / Grafana:

```
backup_age=19.4h;48;72;0;
```

## Casos edge contemplados

1. **Log inexistente** → CRITICAL: "Log file not found"
2. **Log vacío** → CRITICAL: "Log file is empty"
3. **Backup interrumpido** (solo línea INICIANDO, sin COMPLETADO ni ERROR) → CRITICAL: "Last backup FAILED"
4. **Email falla después de backup exitoso** → OK: el backup se completó, el error de email se loguea como `WARNING:`
5. **Ruta de backup no existe** → WARNING en el log, el backup continúa con las demás rutas
6. **retention.dat no encontrado en destino temporal** → WARNING en el log, el backup continúa sin ese archivo
7. **Host sin Naemon (retention deshabilitado)** → se saltea `copy_retention()`, el backup solo incluye `[Backup_Paths]`
8. **Script interrumpido por SIGKILL/OOM** → los temporales se limpian al inicio de la próxima corrida
9. **Falla transitoria de S3** → hasta 5 intentos con backoff exponencial antes de declarar falla
10. **Symlinks en rutas de backup** → si `dereference_symlinks = true` (default), se copia el archivo destino en lugar del link. Si `false`, se preserva el symlink (solo funciona si se restaura en el mismo path)

## Retención de backups en S3

La retención de backups antiguos en S3 se maneja con una **lifecycle policy del bucket**, no desde el script. Configurar en AWS Console:

- Bucket → Management → Lifecycle rules
- Regla: expirar objetos bajo `backups/` después de N días (recomendado: 30-90)

## Sudoers

El usuario que ejecuta el script necesita sudo sin contraseña para copiar `retention.dat` (solo si `[Retention] enabled = true`):

```
noc_user ALL=(ALL) NOPASSWD: ALL
```

O más restrictivo, si se prefiere:

```
noc_user ALL=(root) NOPASSWD: /usr/bin/cp /var/lib/naemon/retention.dat /home/noc_user/naemon_retention/
noc_user ALL=(root) NOPASSWD: /usr/bin/chown naemon:naemon /home/noc_user/naemon_retention/retention.dat
noc_user ALL=(root) NOPASSWD: /usr/bin/chmod 660 /home/noc_user/naemon_retention/retention.dat
```

Si `enabled = false`, no se necesita sudo para el script.
