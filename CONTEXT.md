# S3 Backup — Contexto del Proyecto

## Descripción

**S3 Backup** es un script de backup genérico que comprime configuraciones en un archivo `.tar.gz`, lo sube a un bucket de AWS S3, envía el resultado por email y mantiene un log local. Diseñado para reutilizarse en distintos tipos de hosts (Naemon, Grafana, servidores NOC, etc.) mediante un archivo de configuración `.conf` por host. Incluye un plugin de monitoreo para Naemon/Nagios que verifica el estado del backup a partir del timestamp y contenido del log.

## Repositorio

GitHub: https://github.com/asampedro/NOC_S3-Backup
El usuario sube los commits manualmente.

## Arquitectura

```
naemon_backup.py          ← Script principal (backup)
  ↓ lee
s3bkp.conf               ← Configuración INI (mismo directorio que el script)
  ↓ produce
/var/log/naemon/s3backup/S3backup.log  ← Log de la última corrida (truncado en cada ejecución)
  ↓ leído por
check_s3_backup.py        ← Plugin de monitoreo Naemon/Nagios
  ↓ reporta a
Naemon                    ← Monitor con estados OK/WARNING/CRITICAL

Modos de monitoreo:
  LOCAL  — plugin lee el log directamente (backup y Naemon en el mismo host)
  NRPE   — Naemon llama al plugin en el host remoto via NRPE
```

## Script principal (`naemon_backup.py`)

### Flujo

1. Carga y valida `s3bkp.conf`. Validación condicional: los campos de `[Retention]` y `tmp_dir_retention` solo son obligatorios si `enabled = true`. Si falta algo, se detiene antes de hacer nada.
2. Limpia directorios temporales (leftovers de corridas interrumpidas).
3. Trunca el log (siempre contiene solo la última corrida).
4. Si `[Retention] enabled = true` → copia `retention.dat` con `sudo` vía `subprocess.run(check=True)`. Si `false` → saltea este paso.
5. Crea `.tar.gz` con las rutas de `[Backup_Paths]`. Si retention está activo, también incluye `retention.dat` del directorio temporal. Rutas inexistentes → `WARNING:` en log. Registra tamaño del archivo.
6. Sube a S3 con retries configurables (`boto3` `Config(retries={'max_attempts': 5, 'mode': 'standard'})`).
7. Verifica subida con `head_object` + comparación de tamaño.
8. Registra `=== BACKUP COMPLETADO EXITOSAMENTE ===` (marcador de éxito).
9. Envía email con el contenido del log. Fallo de email → `WARNING:` (no afecta el estado del backup).
10. Limpia temporales nuevamente (garantizado por `try/finally`).
11. Exit code: `0` (éxito) o `1` (fallo).

### Configuración (`s3bkp.conf`)

Formato INI con `configparser`. Mismo directorio que el script (resuelto vía `__file__` para compatibilidad con cron).

**Secciones obligatorias:**
- `[General]` — service_name (nombre del servicio, aparece en log y email), dereference_symlinks (default: true)
- `[AWS]` — bucket_name, s3_prefix (incluye subfolder por host), region
- `[Proxy]` — http_proxy, https_proxy (obligatorio, sin proxy no hay conexión a AWS)
- `[Paths]` — log_file, tmp_dir (siempre); tmp_dir_retention (solo si retention habilitado)
- `[Backup_Paths]` — path (multilinea, una ruta por línea). retention.dat se auto-incluye, no listar aquí.
- `[Mail]` — smtp_host, from, to

**Secciones opcionales:**
- `[Retention]` — enabled (default: false). Si true, requiere source, owner, group, permissions. Si false o ausente, se saltea la rutina de retention.
- `[S3]` — max_retries (default: 5)

### Ejecución

Crontab del usuario `noc_user`:
```
00 22 * * * python3 /home/noc_user/S3backup/naemon_backup.py
```

El usuario `noc_user` tiene `NOPASSWD: ALL` en sudoers (solo necesario si retention está habilitado).

### Organización del bucket S3

El `s3_prefix` controla la ruta dentro del bucket. Cada host configura su propio subfolder:
- Regional: `backups/naemon/Regional`
- Cross: `backups/naemon/Cross`
- NR: `backups/naemon/NR`
- Otros: `backups/grafana`, `backups/noc-server`, etc.

La retención de backups antiguos se maneja con lifecycle policy del bucket (expiración + transición a Deep Archive), no desde el script.

### Prefijos de log

- `=== INICIANDO BACKUP DE {service_name} ({host}) ===` — inicio
- `Agregado al archive: {path}` — cada ruta incluida en el backup
- `Archivo creado: ...` — info del archivo
- `Backup subido y verificado en S3: ...` — subida OK
- `=== BACKUP COMPLETADO EXITOSAMENTE ===` — **marcador de éxito** (el plugin busca esta línea)
- `ERROR:` — falla del backup (determina CRITICAL en el monitor)
- `WARNING:` — falla no crítica (email, ruta no encontrada) — no afecta el monitoreo

## Plugin de monitoreo (`check_s3_backup.py`)

### Lógica

| Condición | Estado | Code |
|---|---|---|
| Éxito + < 48h | OK | 0 |
| Éxito + 48h–72h | WARNING | 1 |
| Fallado (cualquier edad) | CRITICAL | 2 |
| Éxito + > 72h | CRITICAL | 2 |
| Log inexistente/vacío | CRITICAL | 2 |
| Log ilegible | UNKNOWN | 3 |

Orden: fallado → CRITICAL, >72h → CRITICAL, >48h → WARNING, else OK.

El plugin extrae el hostname con regex genérico `INICIANDO BACKUP DE .+ (hostname)` — funciona con cualquier `service_name`.

Thresholds configurables: `-w 48 -c 72` (default). Incluye performance data `backup_age=Xh;48;72;0;`.

### Configuración Naemon

El monitoreo soporta dos modos:

- **Local:** el plugin lee el log directamente (backup y Naemon en el mismo host). Command: `check_s3_backup`.
- **NRPE:** Naemon llama al plugin en el host remoto via NRPE. Requiere instalar el plugin y NRPE server en el host remoto. Command: `check_s3_backup_nrpe`.

Ver `naemon_config_example.cfg` para ambos ejemplos. Colocar en `/etc/naemon/conf.d/noc/`.

## Decisiones de diseño

- **Proxy obligatorio:** sin proxy no hay conexión a AWS desde la red corporativa. Si falta `[Proxy]`, el script no inicia.
- **`os.system()` → `subprocess.run(check=True)`:** el script original usaba `os.system()` que no detecta fallas. Ahora captura exit codes y stderr.
- **Exit codes:** el script original siempre salía con 0. Ahora devuelve 0 (éxito) o 1 (fallo) para que cron/wrappers detecten fallas.
- **Cleanup doble:** limpia al inicio (leftovers de SIGKILL/crash) + `try/finally` (garantiza limpieza de la corrida actual).
- **Email como WARNING:** el email falla con `WARNING:`, no `ERROR:`. El backup ya se completó; un fallo de email no debe disparar CRITICAL en el monitor.
- **retention.dat auto-incluido:** se copia al dir temporal y se agrega al tar automáticamente. No requiere listarlo en `[Backup_Paths]` (single source of truth).
- **Retention toggle:** `[Retention] enabled = true/false` permite usar el script en hosts sin Naemon. Si `false`, se saltea `copy_retention()` y no requiere los campos de retention ni `tmp_dir_retention`.
- **service_name configurable:** `[General] service_name` reemplaza "NAEMON" hardcodeado en log y email. Permite usar el script en cualquier tipo de host.
- **Plugin regex genérico:** el plugin usa `INICIANDO BACKUP DE .+ (hostname)` para extraer el hostname — funciona con cualquier `service_name` y es backward-compatible con logs antiguos.
- **S3 prefix por host:** la organización del bucket se controla con `s3_prefix` en el conf (Opción A). No requiere cambios en el código. Ej: `backups/naemon/Regional`, `backups/grafana`.
- **Verificación post-upload:** `head_object` + comparación de tamaño para detectar subidas incompletas.
- **Retención en S3:** manejada por lifecycle policy del bucket, no desde el script.
- **Log truncado en cada corrida:** el email ya preserva el contenido de la corrida anterior. No se considera necesario cambiar esto.
- **Log de archivos agregados:** cada ruta incluida en el archive se loguea con `Agregado al archive: {path}`. Da visibilidad de qué entró en el backup.
- **Modo debug (`--debug`):** flag que imprime a stdout el progreso detallado (config cargada, comandos sudo, conexión a S3, verificación, email, cleanup). No afecta el log. Sin el flag, el script corre en silencio (ideal para cron).
- **Colores en modo debug:** colores ANSI en stdout solo cuando es TTY. Bold red = errores, bold green = fin exitoso, bold cyan = inicio, bold yellow = cierre con warnings. Regular green = éxitos de paso, cyan = acciones, yellow = warnings, default = datos/variables. Sin color en log ni email. Reset con `[0m` después de cada línea.
- **Mensaje de cierre:** al final del run en `--debug`, imprime `=== BACKUP FINALIZADO SIN ERRORES ===` (bold green), `=== BACKUP FINALIZADO CON WARNINGS ===` (bold yellow), o `=== BACKUP FINALIZADO CON ERRORES ===` (bold red). Basado en exit_code y HAD_WARNINGS.
- **Verificación de cleanup:** `cleanup_temp()` verifica con `os.path.exists()` después de `shutil.rmtree()`. Si borra OK → `Cleanup OK:` (solo debug). Si falla → `WARNING: No se pudo eliminar` (siempre en el log).
- **Dereference symlinks:** `[General] dereference_symlinks = true/false` (default: true). Cuando true, `tar.dereference = True` resuelve los symlinks y copia el archivo destino en lugar del link. Cuando false, preserva el symlink. Recomendado true para restore en cualquier ubicación.
- **Monitoreo remoto via NRPE:** para backups que corren en hosts distintos al Naemon central, se usa NRPE. El plugin y el log viven en el host remoto; Naemon llama via `check_nrpe`. Alternativas descartadas: `check_by_ssh` (requiere SSH keys), sincronización de logs (punto de falla extra).

## Deployment

El script está desplegado en 3 servidores Naemon, cada uno con su propio `s3bkp.conf`:

| Host | s3_prefix | Monitoreo |
|---|---|---|
| Naemon Regional | `backups/naemon/Regional` | Local |
| Naemon Cross | `backups/naemon/Cross` | NRPE from Regional |
| Naemon NewRelic | `backups/naemon/NR` | NRPE from Regional |

- **Directorio del script:** `/home/noc_user/S3backup/`
- **Config:** `s3bkp.conf` en el mismo directorio que el script
- **Log:** `/var/log/naemon/s3backup/S3backup.log`
- **Crontab:** `00 22 * * * python3 /home/noc_user/S3backup/naemon_backup.py`
- **S3 lifecycle policy:** configurada en el bucket para expiración y transición a Deep Archive
- **Script viejo:** retirado del crontab (reemplazado por el nuevo)

Para nuevos hosts, crear un `s3bkp.conf` con su `service_name`, `s3_prefix` y rutas específicas.

## Archivos del proyecto

| Archivo | En .gitignore | Descripción |
|---|---|---|
| `naemon_backup.py` | No | Script principal de backup |
| `check_s3_backup.py` | No | Plugin de monitoreo Naemon/Nagios |
| `naemon_config_example.cfg` | No | Config de ejemplo para Naemon |
| `s3bkp.conf` | Sí | Config con valores reales |
| `s3bkp.conf.example` | No | Plantilla de config sin valores sensibles |
| `S3backup.log` | Sí | Log de la última corrida |
| `README.md` | No | Documentación |
| `CONTEXT.md` | No | Contexto para sesiones de Toqan |
