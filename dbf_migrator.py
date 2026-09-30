# -*- coding: utf-8 -*-
import logging
import sys
from pathlib import Path

# Ruta del log junto al exe (compatible con PyInstaller)
if getattr(sys, 'frozen', False):
    _BASE_DIR = Path(sys.executable).parent
else:
    _BASE_DIR = Path(__file__).parent

_LOG_FILE = _BASE_DIR / "migrador.log"

logging.basicConfig(
    filename=str(_LOG_FILE),
    level=logging.DEBUG,
    format='%(asctime)s %(levelname)s %(message)s',
    encoding='utf-8'
)
logging.debug('=== INICIO MIGRADOR ===')
import sys
sys._excepthook = sys.excepthook
def exception_hook(exctype, value, traceback):
    logging.critical('CRASH', exc_info=(exctype, value, traceback))
    sys._excepthook(exctype, value, traceback)
sys.excepthook = exception_hook
"""
DBF → MySQL / PostgreSQL Migrator  v2.3
- Normalización de campos por tabla (TABLE_DEFAULTS):
    stock:    bloqueado, sm → 0 si NULL
    acl:      campos ib_*, bloqueo_cc, iva_exen, etc → 0 si NULL
              fechas f_alta, mkfecha, etc → '1900-01-01' si NULL
    ctacte_d: codfact → NULL (campo inexistente en DBFs viejos)
- Auto-fix de columnas con valores fuera de rango (convierte a TEXT automáticamente)
- Guarda y carga configuración de conexión automáticamente (migrator_config.json)
- Reintentar fila por fila si falla el batch
- Manejo de errores mejorado

Requiere:
    py -3.12 -m pip install PyQt6 dbfread mysql-connector-python psycopg2-binary
"""

import sys
import os
import re
import json
import uuid as uuid_lib
import traceback
from pathlib import Path

# ── Captura errores no manejados para que NO se cierre sin aviso ──
def handle_exception(exc_type, exc_value, exc_tb):
    error_msg = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    print("ERROR NO MANEJADO:\n", error_msg)
    try:
        from PyQt6.QtWidgets import QMessageBox, QApplication
        app = QApplication.instance()
        if app:
            box = QMessageBox()
            box.setIcon(QMessageBox.Icon.Critical)
            box.setWindowTitle("Error inesperado")
            box.setText("Ocurrió un error inesperado. Copiá el detalle para reportarlo.")
            box.setDetailedText(error_msg)
            box.exec()
    except Exception:
        pass

sys.excepthook = handle_exception

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QFileDialog, QListWidget, QListWidgetItem,
    QComboBox, QLineEdit, QSpinBox, QCheckBox, QProgressBar,
    QTextEdit, QGroupBox, QFormLayout, QSplitter, QFrame,
    QMessageBox, QTabWidget, QDialog, QScrollArea,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor

# ══════════════════════════════════════════════════════════════════
#  ARCHIVO DE CONFIGURACIÓN
# ══════════════════════════════════════════════════════════════════

CONFIG_FILE = _BASE_DIR / "migrator_config.json"
VERSION_FILE = Path(__file__).parent / "VERSION"


def _load_app_version() -> str:
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip() or "2.3.4"
    except Exception:
        return "2.3.4"


APP_VERSION = _load_app_version()

def load_saved_config() -> dict:
    """Carga la configuración guardada. Devuelve defaults si no existe."""
    defaults = {
        "db_type":  "MySQL",
        "host":     "localhost",
        "port":     3306,
        "user":     "root",
        "password": "",          # vacía por defecto — el usuario la ingresa
        "database": "",          # vacía por defecto — el usuario la ingresa
        "encoding": "latin-1",
        "drop_if_exists": False,
    }
    try:
        if CONFIG_FILE.exists():
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            defaults.update(data)
    except Exception:
        pass
    return defaults

def save_config(cfg: dict):
    """Persiste la configuración incluyendo la contraseña."""
    try:
        CONFIG_FILE.write_text(
            json.dumps(cfg, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
    except Exception:
        pass

def save_files(files: list) -> bool:
    """Guarda la lista de archivos DBF en la config. Devuelve True si OK."""
    try:
        data = {}
        if CONFIG_FILE.exists():
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        data["dbf_files"] = [str(f) for f in files]
        CONFIG_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return True
    except Exception as e:
        print(f"[save_files ERROR] {e} — config: {CONFIG_FILE}")
        return False

def load_saved_files() -> list:
    """Carga la lista de archivos DBF guardada, filtrando los que ya no existen."""
    try:
        if CONFIG_FILE.exists():
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            all_files  = data.get("dbf_files", [])
            existentes = [f for f in all_files if Path(f).exists()]
            if len(existentes) < len(all_files):
                faltantes = len(all_files) - len(existentes)
                print(f"[load_saved_files] {faltantes} archivo(s) ya no existen en disco — ignorados")
            return existentes
    except Exception as e:
        print(f"[load_saved_files ERROR] {e} — config: {CONFIG_FILE}")
    return []


# ══════════════════════════════════════════════════════════════════
#  CAMPOS DE CONTROL
# ══════════════════════════════════════════════════════════════════

SYNC_FIELDS_MYSQL = """\
  `_id`              BIGINT        NOT NULL AUTO_INCREMENT,
  `_uuid`            CHAR(36)      NOT NULL DEFAULT (UUID()),
  `_creado_en`       DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `_modificado_en`   DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
                                   ON UPDATE CURRENT_TIMESTAMP(3),
  `_sincronizado_en` DATETIME(3)   NULL,
  `_sync_estado`     TINYINT(1)    NOT NULL DEFAULT 0,
  `_sync_version`    INT           NOT NULL DEFAULT 1,
  `_origen`          VARCHAR(20)   NOT NULL DEFAULT 'MIGRADOR',
  `_eliminado`       TINYINT(1)    NOT NULL DEFAULT 0,
  `_eliminado_en`    DATETIME(3)   NULL,
  `_hash`            CHAR(32)      NULL,
  `_api_id`          VARCHAR(100)  NULL,
  `update_at`        DATETIME      NULL,
  `syncro_at`        DATETIME      NULL,
  `deleted_at`       DATETIME      NULL,
  PRIMARY KEY (`_id`)"""

SYNC_FIELDS_PG = """\
  _id              BIGSERIAL     PRIMARY KEY,
  _uuid            CHAR(36)      NOT NULL,
  _creado_en       TIMESTAMP(3)  NOT NULL DEFAULT NOW(),
  _modificado_en   TIMESTAMP(3)  NOT NULL DEFAULT NOW(),
  _sincronizado_en TIMESTAMP(3)  NULL,
  _sync_estado     SMALLINT      NOT NULL DEFAULT 0,
  _sync_version    INT           NOT NULL DEFAULT 1,
  _origen          VARCHAR(20)   NOT NULL DEFAULT 'MIGRADOR',
  _eliminado       SMALLINT      NOT NULL DEFAULT 0,
  _eliminado_en    TIMESTAMP(3)  NULL,
  _hash            CHAR(32)      NULL,
  _api_id          VARCHAR(100)  NULL,
  update_at        TIMESTAMP     NULL,
  syncro_at        TIMESTAMP     NULL,
  deleted_at       TIMESTAMP     NULL"""

# OJO: MySQL NO soporta "CREATE INDEX IF NOT EXISTS" (eso es MariaDB y
# PostgreSQL). Estas plantillas lo usaban, asi que en MySQL fallaban las cuatro
# por error de sintaxis y el except de mas abajo se lo tragaba en silencio: el
# log decia "0/4 indices creados" y nadie lo miraba. Por eso ninguna tabla
# tenia indices de sincronizacion. Si el indice ya existe, MySQL devuelve 1061
# (Duplicate key name) y eso se trata como exito.
#
# idx_sync es compuesto y se llama igual que en la migracion v45 de
# migrations.json, para que una tabla creada por el migrador y una arreglada
# por la migracion queden identicas.
SYNC_INDEXES_MYSQL = [
    "CREATE UNIQUE INDEX `idx_{t}_uuid` ON `{t}` (`_uuid`)",
    "CREATE        INDEX `idx_{t}_mod`  ON `{t}` (`_modificado_en`)",
    "CREATE        INDEX `idx_sync`     ON `{t}` (`_sync_estado`, `_eliminado`)",
]

# Tablas en las que el ERP escribe directamente en MySQL, sacadas de los
# INSERT/UPDATE/DELETE de mysql_facturacion.prg, funciones_mysql.prg y
# funciones_mysql_ale.prg.
#
# Para que sirve: "Sugerir desde schema" recorria SOLO las tablas de
# schema.json, asi que una tabla en la que el ERP escribe pero que no esta en
# el schema era invisible. El migrador nunca la creaba y cada INSERT del ERP
# moria con 1146 "Table doesn't exist", en silencio salvo una linea en Audita.
# Fue exactamente el caso de perc_ret: graba_mysql_perc_ret() tiene siete
# lugares que la llaman en el facturador y la tabla no existia en MySQL.
#
# Al agregar una tabla nueva a los PRG, agregarla tambien aca: el boton avisa
# cuando una de estas no esta en el schema.
TABLAS_QUE_ESCRIBE_EL_ERP = [
    "af", "af_cobros", "af_obs", "afd", "bancos", "cheques", "comision",
    "contable", "ctacte_d", "ctacte_h", "depo_st", "mov_caja", "mstock",
    "perc_ret", "recibo_aplica", "recibo_cab", "recibo_cobro", "rem",
    "rem_d", "stock", "tarje",
]

SYNC_INDEXES_PG = [
    'CREATE UNIQUE INDEX IF NOT EXISTS "idx_{t}_uuid"        ON "{t}" (_uuid)',
    'CREATE        INDEX IF NOT EXISTS "idx_{t}_mod"         ON "{t}" (_modificado_en)',
    'CREATE        INDEX IF NOT EXISTS "idx_sync"            ON "{t}" (_sync_estado, _eliminado)',
]

CAMPOS_INFO = [
    ("_id",              "BIGINT AUTO_INCREMENT PK"),
    ("_uuid",            "CHAR(36) UNIQUE — ID global"),
    ("_creado_en",       "DATETIME(3) — fecha creación"),
    ("_modificado_en",   "DATETIME(3) — última modificación"),
    ("_sincronizado_en", "DATETIME(3) NULL — último sync"),
    ("_sync_estado",     "0=pend  1=ok  2=error  3=conflicto"),
    ("_sync_version",    "INT — versión del registro"),
    ("_origen",          "VFP | API | MIGRADOR"),
    ("_eliminado",       "Baja lógica 0/1"),
    ("_eliminado_en",    "DATETIME(3) NULL"),
    ("_hash",            "MD5 del contenido"),
    ("_api_id",          "ID asignado por la API"),
    ("update_at",        "DATETIME NULL — compat. sistemas legacy"),
    ("syncro_at",        "DATETIME NULL — compat. sistemas legacy"),
    ("deleted_at",       "DATETIME NULL — compat. sistemas legacy"),
]


# ══════════════════════════════════════════════════════════════════
#  WORKER — TEST DE CONEXIÓN
# ══════════════════════════════════════════════════════════════════

class ConnectionTestWorker(QThread):
    result = pyqtSignal(bool, str)

    def __init__(self, config: dict):
        super().__init__()
        self.config = config

    def run(self):
        logging.debug('ConnectionTestWorker.run() iniciado')
        cfg = self.config
        try:
            logging.debug(f'Conectando a {cfg["db_type"]} en {cfg["host"]}:{cfg["port"]}')
            if cfg["db_type"] == "MySQL":
                import mysql.connector
                # Intentar conexión probando ambos métodos de autenticación
                # automáticamente — compatible con MySQL 5.7, 8.0 y cualquier config
                conn = None
                ultimo_error = None
                for auth in [None, "mysql_native_password", "caching_sha2_password"]:
                    try:
                        kw = dict(
                            host=cfg["host"], port=int(cfg["port"]),
                            user=cfg["user"], password=cfg["password"],
                            connection_timeout=5, use_pure=True,
                        )
                        if auth:
                            kw["auth_plugin"] = auth
                        conn = mysql.connector.connect(**kw)
                        break  # conectó — salir del loop
                    except Exception as e:
                        ultimo_error = e
                        continue
                if conn is None:
                    raise ultimo_error
                cur = conn.cursor()
                cur.execute("SELECT VERSION()")
                version = cur.fetchone()[0]
                cur.close(); conn.close()
                self.result.emit(True, f"✅ MySQL conectado correctamente\nVersión: {version}")
            else:
                import psycopg2
                conn = psycopg2.connect(
                    host=cfg["host"], port=int(cfg["port"]),
                    user=cfg["user"], password=cfg["password"],
                    dbname="postgres", connect_timeout=5,
                )
                cur = conn.cursor()
                cur.execute("SELECT version()")
                version = cur.fetchone()[0]
                cur.close(); conn.close()
                self.result.emit(True, f"✅ PostgreSQL conectado correctamente\nVersión: {version}")
        except Exception as e:
            self.result.emit(False, f"❌ No se pudo conectar:\n\n{e}\n\n"
                             f"Datos usados:\n"
                             f"  Host: {cfg['host']}:{cfg['port']}\n"
                             f"  Usuario: {cfg['user']}\n"
                             f"  Base: {cfg.get('database', '(sin base)')}\n\n"
                             "Verificá host, puerto, usuario y contraseña.")


# ══════════════════════════════════════════════════════════════════
#  WORKER — MIGRACIÓN
# ══════════════════════════════════════════════════════════════════

class MigrationWorker(QThread):
    progress   = pyqtSignal(int)
    log        = pyqtSignal(str)
    table_done = pyqtSignal(str, int)
    finished   = pyqtSignal(bool, str)

    def __init__(self, config: dict, files: list):
        super().__init__()
        self.config = config
        self.files  = files
        self._defaults_logged = set()  # para loguear cada default solo una vez

    # ── Conexión ──────────────────────────────────────────────────
    def _get_connection(self, dbname=None):
        cfg = self.config
        if cfg["db_type"] == "MySQL":
            import mysql.connector
            # Intentar con ambos métodos — compatible con cualquier versión MySQL
            ultimo_error = None
            for auth in [None, "mysql_native_password", "caching_sha2_password"]:
                try:
                    kw = dict(host=cfg["host"], port=int(cfg["port"]),
                              user=cfg["user"], password=cfg["password"],
                              connection_timeout=10, use_pure=True)
                    if dbname:
                        kw["database"] = dbname
                    if auth:
                        kw["auth_plugin"] = auth
                    return mysql.connector.connect(**kw)
                except Exception as e:
                    ultimo_error = e
                    continue
            raise ultimo_error
        else:
            import psycopg2
            return psycopg2.connect(
                host=cfg["host"], port=int(cfg["port"]),
                user=cfg["user"], password=cfg["password"],
                dbname=dbname or "postgres", connect_timeout=10,
            )

    def _ensure_database(self, db_name: str):
        cfg  = self.config
        safe = re.sub(r"[^\w]", "_", db_name)
        if cfg["db_type"] == "MySQL":
            conn = self._get_connection()
            cur  = conn.cursor()
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{safe}` "
                        f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
            cur.close(); conn.close()
            return self._get_connection(safe)
        else:
            conn = self._get_connection()
            conn.autocommit = True
            cur  = conn.cursor()
            cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (safe,))
            if not cur.fetchone():
                cur.execute(f'CREATE DATABASE "{safe}" ENCODING \'UTF8\'')
            cur.close(); conn.close()
            return self._get_connection(safe)

    # ── Mapeo de tipos ────────────────────────────────────────────
    def _leer_nombres_desde_dbc(self, carpeta: str) -> dict:
        """Lee nombres largos de campos desde el .dbc de FoxPro."""
        resultado = {}
        try:
            # Buscar el .dbc en la carpeta usando listdir
            dbcs = []
            try:
                for f in os.listdir(carpeta):
                    if f.lower().endswith(".dbc"):
                        dbcs.append(os.path.join(carpeta, f))
            except Exception as e:
                self._dbc_log = f"[DBC] Error listando carpeta: {e}"
                return {}

            if not dbcs:
                self._dbc_log = f"[DBC] No hay .dbc en: {carpeta}"
                return {}

            dbc_path = dbcs[0]
            self._dbc_log = f"[DBC] Abriendo: {dbc_path}"

            from dbfread import DBF
            dbc = DBF(dbc_path, encoding="latin-1", load=True,
                      ignore_missing_memofile=True)
            recs = list(dbc)

            # Construir mapa objectid → nombre_tabla
            tablas = {
                r["OBJECTID"]: str(r["OBJECTNAME"] or "").strip().lower()
                for r in recs if r["OBJECTTYPE"] == "Table"
            }

            # Extraer campos con nombre largo (>10 chars)
            for r in recs:
                if r["OBJECTTYPE"] != "Field":
                    continue
                nombre_largo = str(r["OBJECTNAME"] or "").strip().lower()
                if not nombre_largo or len(nombre_largo) <= 10:
                    continue
                tabla = tablas.get(r["PARENTID"])
                if not tabla:
                    continue
                nombre_corto = nombre_largo[:10]
                resultado.setdefault(tabla, {})[nombre_corto] = nombre_largo

            total = sum(len(v) for v in resultado.values())
            self._dbc_log += f" → {total} nombres largos en {len(resultado)} tablas"

        except Exception as e:
            self._dbc_log = f"[DBC] Error: {e}"

        return resultado

    def _dbf_to_sql(self, field):
        """
        Mapea tipos DBF/VFP a tipos MySQL/PostgreSQL respetando
        exactamente el largo y decimales definidos en el DBF.

        DBF type reference:
          C = Character  → VARCHAR(n)
          N = Numeric    → DECIMAL(length, decimal_count)  siempre
          F = Float      → DOUBLE / DOUBLE PRECISION
          D = Date       → DATE
          T = DateTime   → DATETIME(3) / TIMESTAMP(3)
          L = Logical    → TINYINT(1) / SMALLINT  (0 o 1)
          M = Memo       → LONGTEXT / TEXT
          B = Binary     → LONGBLOB / BYTEA
          I = Integer    → INT
          Y = Currency   → DECIMAL(19,4)
          +/= = AutoInc  → INT
        """
        mysql = self.config["db_type"] == "MySQL"
        t = field.type
        l = field.length
        d = field.decimal_count

        if t == "C":
            # Character: respetar largo exacto del DBF
            return f"VARCHAR({max(l, 1)})"

        elif t == "N":
            # Numeric: SIEMPRE DECIMAL(length, decimal_count)
            # Nunca BIGINT — eso ignora el rango definido en el DBF
            # Ej: N(6,0) → DECIMAL(6,0), N(12,2) → DECIMAL(12,2)
            safe_l = max(l, 1)
            safe_d = max(d, 0)
            # DECIMAL en MySQL: máximo precision=65
            if safe_l > 65:
                safe_l = 65
            return f"DECIMAL({safe_l},{safe_d})"

        elif t == "F":
            # Float: punto flotante de doble precisión
            return "DOUBLE" if mysql else "DOUBLE PRECISION"

        elif t == "D":
            # Date
            return "DATE"

        elif t == "T":
            # DateTime
            return "DATETIME(3)" if mysql else "TIMESTAMP(3)"

        elif t == "L":
            # Logical: 0 o 1
            return "TINYINT(1)" if mysql else "SMALLINT"

        elif t == "M":
            # Memo: texto largo
            return "LONGTEXT" if mysql else "TEXT"

        elif t == "B":
            # Binary
            return "LONGBLOB" if mysql else "BYTEA"

        elif t in ("I", "+", "="):
            # Integer / AutoIncrement VFP
            return "INT"

        elif t == "Y":
            # Currency VFP
            return "DECIMAL(19,4)"

        elif t == "W":
            # Blob VFP
            return "LONGBLOB" if mysql else "BYTEA"

        elif t == "G":
            # General (OLE objects) — guardar como blob
            return "LONGBLOB" if mysql else "BYTEA"

        else:
            # Cualquier otro tipo desconocido → TEXT seguro
            return "TEXT"

    # ── Normalización por tabla ───────────────────────────────────
    # Campos que deben tener un valor por defecto cuando vienen NULL del DBF.
    # Formato: { nombre_tabla: { nombre_campo: valor_default } }
    # Esto resuelve problemas que se originan en versiones viejas del DBF
    # donde ciertos campos aún no existían o siempre eran vacíos.
    TABLE_DEFAULTS = {
        "stock": {
            "bloqueado": 0,
            "sm":        0,
        },
        "acl": {
            "bloqueo_cc": 0,
            "iva_exen":   0,
            "ib_exen":    0,
            "liberado":   0,
            "impri_eti":  0,
            "tcompras":   0,
            "lote":       0,
            # campos ib_ — todos default 0
            "ib_exen_901": 0, "ib_porc_901": 0,
            "ib_exen_921": 0, "ib_porc_921": 0,
            "ib_exen_917": 0, "ib_porc_917": 0,
            "ib_exen_914": 0, "ib_porc_914": 0,
            "ib_exen_924": 0, "ib_porc_924": 0,
            "ib_porc":     0,
            "iva_porc":    0,
            # fechas obligatorias — usar fecha mínima si vienen NULL
            "f_alta":    "1900-01-01",
            "mkfecha":   "1900-01-01",
            "mkfechaul": "1900-01-01",
            "fcredito":  "1900-01-01",
            "ulti_ope":  "1900-01-01",
        },
        "ctacte_d": {
            # codfact no existía en versiones viejas del DBF → NULL
            "codfact": None,
        },
    }

    # ── Overrides de tipo por columna ────────────────────────────
    # Cuando el DBF define un campo con un tamaño insuficiente para MySQL,
    # acá forzamos el tipo correcto. Se aplica durante el CREATE TABLE.
    # Formato: { nombre_tabla: { nombre_campo: "TIPO_SQL" } }
    TABLE_COLUMN_OVERRIDES = {
        "af": {
            "dolar": "DECIMAL(10,4)",   # DBF N(7,4) no alcanza para cotizaciones > 999
            "plazo": "VARCHAR(20)",     # DBF C(15) se queda corto con el formato usado
        },
        "afd": {
            "dolar": "DECIMAL(10,4)",
        },
        "contable": {
            "dolar": "DECIMAL(10,4)",
        },
        "rem": {
            "dolar": "DECIMAL(10,4)",
        },
        "rem_d": {
            "dolar": "DECIMAL(10,4)",
        },
        "mov_caja": {
            "dolar": "DECIMAL(10,4)",
        },
    }

    def _apply_table_defaults(self, tname, row, col_names, _diag_logged=None):
        """
        Aplica defaults por tabla a los campos NULL que lo requieran.
        Se llama despues de _safe() para cada fila antes de insertar.
        """
        defaults = self.TABLE_DEFAULTS.get(tname)
        if not defaults:
            return row
        if _diag_logged is None:
            _diag_logged = self._defaults_logged
        row = list(row)
        for i, col in enumerate(col_names):
            if col in defaults and row[i] is None:
                row[i] = defaults[col]
                key = f"{tname}.{col}"
                if key not in _diag_logged:
                    self.log.emit(f"  🔧 DEFAULT aplicado: {tname}.{col} = {defaults[col]!r}")
                    _diag_logged.add(key)
        return tuple(row)

    # ── Limpieza de valores ───────────────────────────────────────
    def _safe(self, val, field=None):
        """
        Limpia y convierte valores del DBF antes de insertar.
        Respeta el tipo y rango definido en la estructura del DBF.
        """
        # ── Campos lógicos: NULL/vacío en DBF = False = 0 ─────────
        if field and field.type == "L":
            if val is None:
                return 0
            if isinstance(val, bool):
                return int(val)
            if isinstance(val, int):
                return 1 if val else 0
            if isinstance(val, str):
                return 1 if val.upper() in ("T", "Y", "S", "1") else 0
            return 0

        # ── Campos Date: NULL en DBF = None (permitido en MySQL) ──
        # Se usa None para que MySQL guarde NULL en DATE.
        # El sincronizador maneja los campos DATE obligatorios con setdefault.
        if field and field.type == "D":
            if val is None:
                return None
            return val

        if val is None:           return None
        if isinstance(val, bool): return int(val)

        if field and field.type == "N":
            # Numérico DBF → DECIMAL(length, decimal_count) en MySQL
            # Calcular el rango máximo permitido por el DECIMAL del DBF
            # Ej: N(6,0) → máximo 999999, N(12,2) → máximo 9999999999.99
            try:
                from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
                if val is None:
                    return None
                d = Decimal(str(val))
                # Verificar NaN o Infinito
                if not d.is_finite():
                    return None
                l = field.length
                dec = field.decimal_count
                # Máximo valor que cabe en DECIMAL(l, dec)
                int_digits = l - dec - (1 if dec > 0 else 0)
                if int_digits < 1:
                    int_digits = 1
                max_val = Decimal(10 ** int_digits) - Decimal(10 ** -dec if dec > 0 else 1)
                # Si excede el rango → None (se logeará como omitido)
                if abs(d) > max_val:
                    return None
                return val
            except (TypeError, ValueError, Exception):
                return None

        if field and field.type == "F":
            # Float → DOUBLE
            try:
                v = float(val)
                if v != v or abs(v) == float("inf"):
                    return None
                return v
            except (TypeError, ValueError):
                return None

        return val

    # ── Auto-fix de columnas problemáticas ───────────────────────
    def _extract_bad_column(self, error_msg: str) -> str:
        """Extrae el nombre de columna del error MySQL 1264."""
        m = re.search(r"column '(\w+)'", str(error_msg), re.IGNORECASE)
        return m.group(1).lower() if m else ""

    def _fix_column(self, cur, conn, tname: str, col: str):
        """Convierte una columna a TEXT para aceptar cualquier valor."""
        try:
            cur.execute(f"ALTER TABLE `{tname}` MODIFY COLUMN `{col}` TEXT")
            conn.commit()
            self.log.emit(f"  🔧 Auto-fix: columna '{col}' → TEXT")
        except Exception as e:
            conn.rollback()
            self.log.emit(f"  ⚠️  No se pudo convertir '{col}': {e}")

    # ── Inserción con auto-fix y fallback fila por fila ──────────
    def _insert_batch(self, cur, conn, sql_ins: str, batch: list,
                      tname: str, col_names: list) -> tuple:
        """
        Inserta un batch. Si falla por rango:
          1. Detecta la columna problemática
          2. La convierte a TEXT automáticamente
          3. Reintenta el batch completo
          4. Si sigue fallando → inserta fila por fila
        Devuelve (insertados_ok, insertados_error)
        """
        # ── Intento 1: batch completo ─────────────────────────────
        try:
            cur.executemany(sql_ins, batch)
            conn.commit()
            return len(batch), 0
        except Exception as e:
            conn.rollback()
            err_str = str(e)

        # ── Auto-fix: convertir columna problemática ──────────────
        fixed_cols = set()
        for _ in range(10):  # máximo 10 columnas a fixear por batch
            if "Out of range" not in err_str and "1264" not in err_str:
                break
            bad = self._extract_bad_column(err_str)
            if not bad or bad in fixed_cols or bad not in col_names:
                break
            self._fix_column(cur, conn, tname, bad)
            fixed_cols.add(bad)

            # Reintentar batch después del fix
            try:
                cur.executemany(sql_ins, batch)
                conn.commit()
                return len(batch), 0
            except Exception as e2:
                conn.rollback()
                err_str = str(e2)

        # ── Fallback: fila por fila ───────────────────────────────
        ok = 0; err = 0
        for row in batch:
            inserted = False
            row_err = ""
            for _ in range(5):  # hasta 5 intentos de fix por fila
                try:
                    cur.execute(sql_ins, row)
                    conn.commit()
                    ok += 1
                    inserted = True
                    break
                except Exception as e3:
                    conn.rollback()
                    row_err = str(e3)
                    if "Out of range" in row_err or "1264" in row_err:
                        bad = self._extract_bad_column(row_err)
                        if bad and bad not in fixed_cols and bad in col_names:
                            self._fix_column(cur, conn, tname, bad)
                            fixed_cols.add(bad)
                            continue
                    break  # error distinto → no reintentar
            if not inserted:
                err += 1

        return ok, err

    # ── Punto de entrada ──────────────────────────────────────────
    def run(self):
        try:
            self._do_migration()
        except Exception as e:
            self.log.emit(f"\n❌ Error crítico:\n{traceback.format_exc()}")
            self.finished.emit(False, f"❌ Error: {e}")

    def _do_migration(self):
        from dbfread import DBF
        cfg     = self.config
        mysql   = cfg["db_type"] == "MySQL"
        total   = len(self.files)
        db_name = re.sub(r"[^\w]", "_", cfg["database"])

        self.log.emit(f"🔌 Conectando a {cfg['db_type']} en {cfg['host']}:{cfg['port']} …")
        conn = self._ensure_database(db_name)
        self.log.emit(f"✅ Conexión OK  |  Base: '{db_name}'")
        cur  = conn.cursor()

        # Leer nombres largos del .dbc una sola vez antes del loop
        carpeta_dbf = str(Path(self.files[0]).parent) if self.files else ""
        self._dbc_log = ""
        nombres_dbc = self._leer_nombres_desde_dbc(carpeta_dbf)
        if nombres_dbc:
            total_largos = sum(len(v) for v in nombres_dbc.values())
            self.log.emit(f"📝 DBC encontrado — {total_largos} nombres largos en {len(nombres_dbc)} tablas")
        else:
            self.log.emit("ℹ️  Sin .dbc — usando nombres de campo originales (máx 10 chars)")

        for idx, filepath in enumerate(self.files):
            tname    = re.sub(r"[^\w]", "_", Path(filepath).stem).lower()
            encoding = cfg.get("encoding", "latin-1")

            self.log.emit(f"\n📂 [{idx+1}/{total}]  {Path(filepath).name}  →  tabla '{tname}'")

            try:
                dbf = DBF(filepath, encoding=encoding, load=True,
                          ignore_missing_memofile=True)
            except Exception as e:
                self.log.emit(f"  ⚠️  No se pudo leer el DBF: {e}")
                self.progress.emit(int((idx+1)/total*100))
                continue

            # Mapeo de nombres cortos → largos para esta tabla (desde DBC)
            _map_nombres = nombres_dbc.get(tname, {})

            def _nc(f):
                """Nombre del campo — largo si existe en DBC, corto si no."""
                return _map_nombres.get(f.name.lower(), f.name.lower())

            if _map_nombres:
                logging.debug(f"[DBC] {tname}: {_map_nombres}")

            try:
                dbf = DBF(filepath, encoding=encoding, load=True,
                          ignore_missing_memofile=True)
            except Exception as e:
                self.log.emit(f"  ⚠️  No se pudo leer el DBF: {e}")
                self.progress.emit(int((idx+1)/total*100))
                continue

            # DROP
            if cfg.get("drop_if_exists"):
                q = (f"DROP TABLE IF EXISTS `{tname}`" if mysql
                     else f'DROP TABLE IF EXISTS "{tname}"')
                cur.execute(q); conn.commit()
                self.log.emit("  🗑️  Tabla eliminada (DROP IF EXISTS)")

            # CREATE TABLE
            # Columnas extra por tabla (campos que no existen en el DBF viejo)
            tbl_extra = {
                k: v for k, v in self.TABLE_DEFAULTS.get(tname, {}).items()
                if k not in [_nc(f) for f in dbf.fields]
            }

            def _extra_col_sql(col, val, is_mysql):
                """Infiere el tipo SQL del valor default para columnas extra."""
                if val is None:
                    return f"`{col}` DECIMAL(15,2) NULL" if is_mysql else f'"{col}" DECIMAL(15,2) NULL'
                if isinstance(val, str) and "-" in str(val):  # fecha '1900-01-01'
                    return f"`{col}` DATE NULL" if is_mysql else f'"{col}" DATE NULL'
                if isinstance(val, int):
                    return f"`{col}` TINYINT(1) NOT NULL DEFAULT {val}" if is_mysql else f'"{col}" SMALLINT NOT NULL DEFAULT {val}'
                return f"`{col}` TEXT NULL" if is_mysql else f'"{col}" TEXT NULL'

            try:
                if mysql:
                    _overrides = self.TABLE_COLUMN_OVERRIDES.get(tname, {})
                    dbf_cols = ",\n  ".join(
                        f"`{_nc(f)}` {_overrides.get(_nc(f), self._dbf_to_sql(f))}"
                        for f in dbf.fields)
                    extra_cols = ",\n  ".join(
                        _extra_col_sql(c, v, True) for c, v in tbl_extra.items())
                    all_col_defs = dbf_cols + (f",\n  {extra_cols}" if extra_cols else "")
                    ddl = (f"CREATE TABLE IF NOT EXISTS `{tname}` (\n"
                           f"  {all_col_defs},\n  {SYNC_FIELDS_MYSQL}\n"
                           f") CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
                else:
                    _overrides = self.TABLE_COLUMN_OVERRIDES.get(tname, {})
                    dbf_cols = ",\n  ".join(
                        f'"{_nc(f)}" {_overrides.get(_nc(f), self._dbf_to_sql(f))}'
                        for f in dbf.fields)
                    extra_cols = ",\n  ".join(
                        _extra_col_sql(c, v, False) for c, v in tbl_extra.items())
                    all_col_defs = dbf_cols + (f",\n  {extra_cols}" if extra_cols else "")
                    ddl = (f'CREATE TABLE IF NOT EXISTS "{tname}" (\n'
                           f"  {all_col_defs},\n  {SYNC_FIELDS_PG}\n)")

                cur.execute(ddl); conn.commit()
                extra_info = f" + {len(tbl_extra)} cols extra" if tbl_extra else ""
                self.log.emit(f"  ✅ Tabla creada  "
                              f"({len(dbf.fields)} cols DBF{extra_info} + 15 campos de control)")
            except Exception as e:
                self.log.emit(f"  ❌ Error creando tabla '{tname}': {e}")
                conn.rollback()
                self.progress.emit(int((idx+1)/total*100))
                continue

            # ÍNDICES
            indexes = SYNC_INDEXES_MYSQL if mysql else SYNC_INDEXES_PG
            idx_ok  = 0
            for tpl in indexes:
                sql_idx = tpl.format(t=tname)
                try:
                    cur.execute(sql_idx); conn.commit(); idx_ok += 1
                except Exception as e:
                    conn.rollback()
                    # 1061 = Duplicate key name: el indice ya estaba, es exito.
                    # Cualquier otra cosa hay que MOSTRARLA: antes este except
                    # se tragaba hasta los errores de sintaxis y por eso nunca
                    # se crearon los indices en ningun cliente.
                    if "1061" in str(e) or "duplicate key name" in str(e).lower():
                        idx_ok += 1
                    else:
                        self.log.emit(f"  ⚠️ No se pudo crear el índice: {e}")
                        self.log.emit(f"     SQL: {sql_idx}")
            self.log.emit(f"  📌 {idx_ok}/{len(indexes)} índices creados")

            # INSERT
            # col_names = columnas con nombres largos si DBC disponible
            col_names = [_nc(f) for f in dbf.fields]

            # Columnas extra: campos en TABLE_DEFAULTS que NO están en el DBF
            # (campos que se agregaron después en VFP y la versión vieja no los tiene)
            extra_defaults = {}
            tbl_defs = self.TABLE_DEFAULTS.get(tname, {})
            for campo, valor in tbl_defs.items():
                if campo not in col_names:
                    extra_defaults[campo] = valor
                    if f"{tname}.{campo}(extra)" not in self._defaults_logged:
                        self.log.emit(f"  🔧 COLUMNA EXTRA: {tname}.{campo} = {valor!r} (no existe en DBF)")
                        self._defaults_logged.add(f"{tname}.{campo}(extra)")

            # Construir INSERT incluyendo columnas extra
            all_cols = col_names + list(extra_defaults.keys())
            if mysql:
                ph      = ", ".join(["%s"] * len(all_cols))
                cs      = ", ".join(f"`{c}`" for c in all_cols)
                sql_ins = (f"INSERT INTO `{tname}` ({cs}, `_uuid`, `_origen`) "
                           f"VALUES ({ph}, %s, 'MIGRADOR')")
            else:
                ph      = ", ".join(["%s"] * len(all_cols))
                cs      = ", ".join(f'"{c}"' for c in all_cols)
                sql_ins = (f'INSERT INTO "{tname}" ({cs}, _uuid, _origen) '
                           f"VALUES ({ph}, %s, 'MIGRADOR')")

            batch = []; batch_sz = 500; row_cnt = 0; err_cnt = 0
            extra_vals = tuple(extra_defaults.values())
            _diag_fila_logged = set()
            for record in dbf:
                row = tuple(self._safe(record[f.name], f) for f in dbf.fields)
                # Diagnóstico: loguear valor real de campos con default ANTES de aplicar
                tbl_defs_check = self.TABLE_DEFAULTS.get(tname, {})
                for i, col in enumerate(col_names):
                    if col in tbl_defs_check and col not in _diag_fila_logged:
                        self.log.emit(
                            f"  🔍 DIAG {tname}.{col}: val={row[i]!r} type={type(row[i]).__name__}"
                        )
                        _diag_fila_logged.add(col)
                        if len(_diag_fila_logged) >= 10:
                            break
                row = self._apply_table_defaults(tname, row, col_names)
                # Agregar valores de columnas extra al final de la fila
                row = row + extra_vals
                batch.append(row + (str(uuid_lib.uuid4()),))
                if len(batch) >= batch_sz:
                    ok, err = self._insert_batch(cur, conn, sql_ins, batch, tname, col_names)
                    row_cnt += ok; err_cnt += err
                    self.log.emit(f"  ↳ {row_cnt:,} filas insertadas …")
                    batch = []

            if batch:
                ok, err = self._insert_batch(cur, conn, sql_ins, batch, tname, col_names)
                row_cnt += ok; err_cnt += err

            if err_cnt:
                self.log.emit(f"  ⚠️  {err_cnt:,} filas omitidas por datos inválidos irrecuperables")

            self.log.emit(f"  ✅ {row_cnt:,} filas insertadas")
            self.table_done.emit(tname, row_cnt)
            self.progress.emit(int((idx+1)/total*100))

        cur.close(); conn.close()
        self.finished.emit(True, f"✅ Migración completada: {total} tabla(s) procesada(s)")


# ══════════════════════════════════════════════════════════════════
#  MIGRACIONES DE SCHEMA — versionado incremental
#  Cada entrada es un cambio que se aplica UNA SOLA VEZ por base.
#  Para agregar un cambio futuro: añadir un dict al final de la lista
#  con version = (última versión + 1).
# ══════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════
#  MIGRACIONES REMOTAS
#  El migrador descarga la lista de migraciones desde GitHub.
#  Si no hay internet usa el fallback local.
#  Para agregar una migración nueva: editar el JSON en GitHub.
# ══════════════════════════════════════════════════════════════════

SCHEMA_MIGRATIONS_LOCAL = [
    {
        "version":     1,
        "descripcion": "Schema base — punto de partida",
        "sql": [
        ],
    },
    {
        "version":     2,
        "descripcion": "Ampliar dolar DECIMAL(10,4) en AF, AFD, contable, rem, rem_d + plazo VARCHAR(20) en AF",
        "sql": [
            "ALTER TABLE `af`       MODIFY COLUMN `dolar` DECIMAL(10,4)",
            "ALTER TABLE `afd`      MODIFY COLUMN `dolar` DECIMAL(10,4)",
            "ALTER TABLE `contable` MODIFY COLUMN `dolar` DECIMAL(10,4)",
            "ALTER TABLE `rem`      MODIFY COLUMN `dolar` DECIMAL(10,4)",
            "ALTER TABLE `rem_d`    MODIFY COLUMN `dolar` DECIMAL(10,4)",
            "ALTER TABLE `af`       MODIFY COLUMN `plazo` VARCHAR(20)",
        ],
    },
    {
        "version":     3,
        "descripcion": "Ampliar dolar DECIMAL(10,4) en mov_caja",
        "sql": [
            "ALTER TABLE `mov_caja` MODIFY COLUMN `dolar` DECIMAL(10,4)",
        ],
    },
    {
        "version":     4,
        "descripcion": "Corregir ib_porc_921 e ib_porc_917 a DECIMAL(5,2) en acl",
        "sql": [
            "ALTER TABLE `acl` MODIFY COLUMN `ib_porc_921` DECIMAL(5,2) NOT NULL DEFAULT '0'",
            "ALTER TABLE `acl` MODIFY COLUMN `ib_porc_917` DECIMAL(5,2) NOT NULL DEFAULT '0'",
        ],
    },
    {
        "version":     5,
        "descripcion": "Agregar codfact, ptovta, nro, codigo en tarje, mov_caja, bancos, cheques",
        "sql": [
            "ALTER TABLE `tarje`    ADD COLUMN `codfact` DECIMAL(3,0) NULL",
            "ALTER TABLE `tarje`    ADD COLUMN `ptovta`  DECIMAL(5,0) NULL",
            "ALTER TABLE `tarje`    ADD COLUMN `nro`     DECIMAL(8,0) NULL",
            "ALTER TABLE `tarje`    ADD COLUMN `codigo`  VARCHAR(4)   NULL",
            "ALTER TABLE `mov_caja` ADD COLUMN `codfact` DECIMAL(3,0) NULL",
            "ALTER TABLE `mov_caja` ADD COLUMN `ptovta`  DECIMAL(5,0) NULL",
            "ALTER TABLE `mov_caja` ADD COLUMN `nro`     DECIMAL(8,0) NULL",
            "ALTER TABLE `mov_caja` ADD COLUMN `codigo`  VARCHAR(4)   NULL",
            "ALTER TABLE `bancos`   ADD COLUMN `codfact` DECIMAL(3,0) NULL",
            "ALTER TABLE `bancos`   ADD COLUMN `ptovta`  DECIMAL(5,0) NULL",
            "ALTER TABLE `bancos`   ADD COLUMN `nro`     DECIMAL(8,0) NULL",
            "ALTER TABLE `bancos`   ADD COLUMN `codigo`  VARCHAR(4)   NULL",
            "ALTER TABLE `cheques`  ADD COLUMN `codfact` DECIMAL(3,0) NULL",
            "ALTER TABLE `cheques`  ADD COLUMN `ptovta`  DECIMAL(5,0) NULL",
            "ALTER TABLE `cheques`  ADD COLUMN `nro`     DECIMAL(8,0) NULL",
        ],
    },
    {
        "version":     6,
        "descripcion": "Corregir dolar/plazo en ped, pre, ped_local + agregar fchange en ped, pre, ped_d, pre_d",
        "sql": [
            "ALTER TABLE `ped`       MODIFY COLUMN `dolar`   DECIMAL(10,4)",
            "ALTER TABLE `ped`       MODIFY COLUMN `plazo`   VARCHAR(20)",
            "ALTER TABLE `ped`       ADD    COLUMN `fchange`  DATETIME(3) NULL",
            "ALTER TABLE `pre`       MODIFY COLUMN `dolar`   DECIMAL(10,4)",
            "ALTER TABLE `pre`       MODIFY COLUMN `plazo`   VARCHAR(20)",
            "ALTER TABLE `pre`       ADD    COLUMN `fchange`  DATETIME(3) NULL",
            "ALTER TABLE `ped_local` MODIFY COLUMN `dolar`   DECIMAL(10,4)",
            "ALTER TABLE `ped_local` MODIFY COLUMN `plazo`   VARCHAR(20)",
            "ALTER TABLE `ped_d`     ADD    COLUMN `fchange`  DATETIME(3) NULL",
            "ALTER TABLE `pre_d`     ADD    COLUMN `fchange`  DATETIME(3) NULL",
        ],
    },
    {
        "version":     7,
        "descripcion": "Corregir dolar DECIMAL(10,4) en comision",
        "sql": [
            "ALTER TABLE `comision` MODIFY COLUMN `dolar` DECIMAL(10,4)",
        ],
    },
    {
        "version":     8,
        "descripcion": "Ampliar form_nro a VARCHAR(15) en mov_caja, bancos, tarje",
        "sql": [
            "ALTER TABLE `mov_caja` MODIFY COLUMN `form_nro` VARCHAR(15)",
            "ALTER TABLE `bancos`   MODIFY COLUMN `form_nro` VARCHAR(15)",
            "ALTER TABLE `tarje`    MODIFY COLUMN `form_nro` VARCHAR(15)",
        ],
    },
    {
        "version":     9,
        "descripcion": "Ampliar campos de precio a DECIMAL(15,4) en afd, stock, pre_d, ped_d, rem_d",
        "sql": [
            "ALTER TABLE `afd` MODIFY COLUMN `fprec` DECIMAL(15,4)",
            "ALTER TABLE `afd` MODIFY COLUMN `f_pu` DECIMAL(15,4)",
            "ALTER TABLE `afd` MODIFY COLUMN `f_lista` DECIMAL(15,4)",
            "ALTER TABLE `stock` MODIFY COLUMN `plp` DECIMAL(15,4)",
            "ALTER TABLE `stock` MODIFY COLUMN `pu` DECIMAL(15,4)",
            "ALTER TABLE `stock` MODIFY COLUMN `lista1` DECIMAL(15,4)",
            "ALTER TABLE `stock` MODIFY COLUMN `lista2` DECIMAL(15,4)",
            "ALTER TABLE `stock` MODIFY COLUMN `lista3` DECIMAL(15,4)",
            "ALTER TABLE `stock` MODIFY COLUMN `lista4` DECIMAL(15,4)",
            "ALTER TABLE `stock` MODIFY COLUMN `lista5` DECIMAL(15,4)",
            "ALTER TABLE `stock` MODIFY COLUMN `lista6` DECIMAL(15,4)",
            "ALTER TABLE `stock` MODIFY COLUMN `lista7` DECIMAL(15,4)",
            "ALTER TABLE `stock` MODIFY COLUMN `lista8` DECIMAL(15,4)",
            "ALTER TABLE `pre_d` MODIFY COLUMN `fprec` DECIMAL(15,4)",
            "ALTER TABLE `ped_d` MODIFY COLUMN `fprec` DECIMAL(15,4)",
            "ALTER TABLE `rem_d` MODIFY COLUMN `fprec` DECIMAL(15,4)",
        ],
    },
    {
        "version":     10,
        "descripcion": "Agregar tablas rem, rem_d, numem, tarjetas + campos nuevos en numem y tarjetas",
        "sql": [
            "ALTER TABLE `numem` ADD COLUMN `nro_cuenta` VARCHAR(22) NULL",
            "ALTER TABLE `numem` ADD COLUMN `cbu`        VARCHAR(22) NULL",
            "ALTER TABLE `numem` ADD COLUMN `moneda`     VARCHAR(3)  NULL",
            "ALTER TABLE `numem` ADD COLUMN `saldo_act`  DECIMAL(15,2) NULL",
            "ALTER TABLE `numem` ADD COLUMN `activo`     TINYINT(1)  DEFAULT 1",
            "ALTER TABLE `tarjetas` ADD COLUMN `plazo_acred` DECIMAL(3,0) NULL",
            "ALTER TABLE `tarjetas` ADD COLUMN `comision`    DECIMAL(5,2) NULL",
            "ALTER TABLE `tarjetas` ADD COLUMN `tipo`        VARCHAR(1)   NULL",
            "ALTER TABLE `tarjetas` ADD COLUMN `red`         VARCHAR(10)  NULL",
            "ALTER TABLE `tarjetas` ADD COLUMN `activo`      TINYINT(1)   DEFAULT 1",
        ],
    },
    {
        "version":     11,
        "descripcion": "Módulo recibos moderno — tablas recibo_cab, recibo_aplica, recibo_cobro",
        "sql": [
            "-- Tablas nuevas creadas via schema (CREATE TABLE IF NOT EXISTS) — no requiere ALTER TABLE",
        ],
    },
    {
        "version":     12,
        "descripcion": "Agregar campo wasap en acl",
        "sql": [
            "ALTER TABLE `acl` ADD COLUMN `wasap` VARCHAR(20) NULL",
        ],
    },
    {
        "version":     13,
        "descripcion": "Agregar columnas de auditoría faltantes en acl, acl2, stock, depo_st, ctacte_d",
        "sql": [
            "ALTER TABLE `acl` ADD COLUMN `_sync_estado` tinyint(1) NOT NULL DEFAULT '0'",
            "ALTER TABLE `acl` ADD COLUMN `_eliminado`   tinyint(1) NOT NULL DEFAULT '0'",
            "ALTER TABLE `acl` ADD COLUMN `_eliminado_en` datetime(3) DEFAULT NULL",
            "ALTER TABLE `acl` ADD COLUMN `_uuid` char(36) NOT NULL DEFAULT (uuid())",
            "ALTER TABLE `acl` ADD COLUMN `_creado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)",
            "ALTER TABLE `acl` ADD COLUMN `_modificado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3)",
            "ALTER TABLE `acl` ADD COLUMN `_origen` varchar(20) NOT NULL DEFAULT 'MIGRADOR'",
            "ALTER TABLE `acl` ADD COLUMN `_sync_version` int NOT NULL DEFAULT '1'",
            "ALTER TABLE `acl2` ADD COLUMN `_sync_estado` tinyint(1) NOT NULL DEFAULT '0'",
            "ALTER TABLE `acl2` ADD COLUMN `_eliminado`   tinyint(1) NOT NULL DEFAULT '0'",
            "ALTER TABLE `acl2` ADD COLUMN `_eliminado_en` datetime(3) DEFAULT NULL",
            "ALTER TABLE `acl2` ADD COLUMN `_uuid` char(36) NOT NULL DEFAULT (uuid())",
            "ALTER TABLE `acl2` ADD COLUMN `_creado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)",
            "ALTER TABLE `acl2` ADD COLUMN `_modificado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3)",
            "ALTER TABLE `acl2` ADD COLUMN `_origen` varchar(20) NOT NULL DEFAULT 'MIGRADOR'",
            "ALTER TABLE `acl2` ADD COLUMN `_sync_version` int NOT NULL DEFAULT '1'",
            "ALTER TABLE `stock` ADD COLUMN `_sync_estado` tinyint(1) NOT NULL DEFAULT '0'",
            "ALTER TABLE `stock` ADD COLUMN `_eliminado`   tinyint(1) NOT NULL DEFAULT '0'",
            "ALTER TABLE `stock` ADD COLUMN `_eliminado_en` datetime(3) DEFAULT NULL",
            "ALTER TABLE `stock` ADD COLUMN `_uuid` char(36) NOT NULL DEFAULT (uuid())",
            "ALTER TABLE `stock` ADD COLUMN `_creado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)",
            "ALTER TABLE `stock` ADD COLUMN `_modificado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3)",
            "ALTER TABLE `stock` ADD COLUMN `_origen` varchar(20) NOT NULL DEFAULT 'MIGRADOR'",
            "ALTER TABLE `stock` ADD COLUMN `_sync_version` int NOT NULL DEFAULT '1'",
            "ALTER TABLE `depo_st` ADD COLUMN `_sync_estado` tinyint(1) NOT NULL DEFAULT '0'",
            "ALTER TABLE `depo_st` ADD COLUMN `_eliminado`   tinyint(1) NOT NULL DEFAULT '0'",
            "ALTER TABLE `depo_st` ADD COLUMN `_eliminado_en` datetime(3) DEFAULT NULL",
            "ALTER TABLE `depo_st` ADD COLUMN `_uuid` char(36) NOT NULL DEFAULT (uuid())",
            "ALTER TABLE `depo_st` ADD COLUMN `_creado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)",
            "ALTER TABLE `depo_st` ADD COLUMN `_modificado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3)",
            "ALTER TABLE `depo_st` ADD COLUMN `_origen` varchar(20) NOT NULL DEFAULT 'MIGRADOR'",
            "ALTER TABLE `depo_st` ADD COLUMN `_sync_version` int NOT NULL DEFAULT '1'",
            "ALTER TABLE `ctacte_d` ADD COLUMN `_sync_estado` tinyint(1) NOT NULL DEFAULT '0'",
            "ALTER TABLE `ctacte_d` ADD COLUMN `_eliminado`   tinyint(1) NOT NULL DEFAULT '0'",
            "ALTER TABLE `ctacte_d` ADD COLUMN `_eliminado_en` datetime(3) DEFAULT NULL",
            "ALTER TABLE `ctacte_d` ADD COLUMN `_uuid` char(36) NOT NULL DEFAULT (uuid())",
            "ALTER TABLE `ctacte_d` ADD COLUMN `_creado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)",
            "ALTER TABLE `ctacte_d` ADD COLUMN `_modificado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3)",
            "ALTER TABLE `ctacte_d` ADD COLUMN `_origen` varchar(20) NOT NULL DEFAULT 'MIGRADOR'",
            "ALTER TABLE `ctacte_d` ADD COLUMN `_sync_version` int NOT NULL DEFAULT '1'",
        ],
    },
    {
        "version":     14,
        "descripcion": "Agregar PK _id a acl2 (tabla sin clave primaria)",
        "sql": [
            "ALTER TABLE `acl2` ADD COLUMN `_id` BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY FIRST",
        ],
    },
    {
        "version":     15,
        "descripcion": "Crear tablas ped_web_pagos y ped_web_meta para cobros y metadatos de pedidos web",
        "sql": [
            """CREATE TABLE IF NOT EXISTS `ped_web_pagos` (
  `id` int NOT NULL AUTO_INCREMENT,
  `id_ped` int NOT NULL,
  `ml_order_id` varchar(50) DEFAULT NULL,
  `payment_id` varchar(50) NOT NULL,
  `origen` varchar(20) DEFAULT 'ML',
  `forma_pago_cod` varchar(10) DEFAULT 'ML',
  `metodo_pago` varchar(50) DEFAULT NULL,
  `tarjeta_banco` varchar(50) DEFAULT NULL,
  `cuotas` int DEFAULT 1,
  `monto_bruto` decimal(12,2) NOT NULL,
  `comision_pasarela` decimal(10,2) DEFAULT 0.00,
  `impuestos_pasarela` decimal(10,2) DEFAULT 0.00,
  `monto_neto` decimal(12,2) NOT NULL,
  `estado_pago` varchar(20) DEFAULT 'approved',
  `fecha_cobro` datetime DEFAULT NULL,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_ped` (`id_ped`),
  KEY `idx_payment` (`payment_id`),
  KEY `idx_ml_order` (`ml_order_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
            """CREATE TABLE IF NOT EXISTS `ped_web_meta` (
  `id` int NOT NULL AUTO_INCREMENT,
  `id_ped` int NOT NULL,
  `order_id` varchar(50) DEFAULT NULL,
  `comprador_nickname` varchar(100) DEFAULT NULL,
  `comprador_telefono` varchar(50) DEFAULT NULL,
  `envio_calle` varchar(255) DEFAULT NULL,
  `envio_cp` varchar(20) DEFAULT NULL,
  `envio_localidad` varchar(100) DEFAULT NULL,
  `envio_provincia` varchar(100) DEFAULT NULL,
  `carrier_nombre` varchar(100) DEFAULT NULL,
  `tracking_id` varchar(100) DEFAULT NULL,
  `meta_key` varchar(50) DEFAULT NULL,
  `meta_value` text DEFAULT NULL,
  `raw_payload` json DEFAULT NULL,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_ped` (`id_ped`),
  KEY `idx_order` (`order_id`),
  KEY `idx_key` (`meta_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
        ],
    },
]


def _fetch_remote_migrations() -> tuple[list, str]:
    """
    Descarga las migraciones desde GitHub usando Authorization header.
    Retorna (lista_migraciones, fuente) donde fuente es 'remoto' o 'local'.
    """
    try:
        import urllib.request
        import ssl
        ctx   = ssl.create_default_context()
        token = "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"
        url   = "https://raw.githubusercontent.com/surisistemas/migraciones/main/migrations.json"
        req   = urllib.request.Request(
            url,
            headers={
                "Authorization": f"token {token}",
                "User-Agent":    "dbf-migrator/1.0",
                "Cache-Control": "no-cache"
            }
        )
        with urllib.request.urlopen(req, timeout=8, context=ctx) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if not isinstance(data, list) or not data:
            raise ValueError("Formato inválido")
        data.sort(key=lambda m: m["version"])
        return data, "remoto"
    except Exception as e:
        logging.warning(f"No se pudieron descargar migraciones remotas: {e} — usando fallback local")
        return SCHEMA_MIGRATIONS_LOCAL, "local"


# Se carga una vez al iniciar — los workers usan esta lista
SCHEMA_MIGRATIONS, _MIGRATIONS_SOURCE = _fetch_remote_migrations()
SCHEMA_VERSION_LATEST = max(m["version"] for m in SCHEMA_MIGRATIONS)


# ── Schema remoto (CREATE TABLE IF NOT EXISTS para bases nuevas) ───
SCHEMA_URL = (
    "https://raw.githubusercontent.com/surisistemas/migraciones/main/schema.json"
)

def _fetch_remote_schema() -> list:
    """Descarga el schema completo desde GitHub."""
    try:
        import urllib.request, ssl
        ctx   = ssl.create_default_context()
        token = "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"
        req   = urllib.request.Request(
            SCHEMA_URL,
            headers={"Authorization": f"token {token}",
                     "User-Agent": "dbf-migrator/1.0",
                     "Cache-Control": "no-cache"}
        )
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if not isinstance(data, list):
            raise ValueError("Formato inválido")
        return data
    except Exception as e:
        logging.warning(f"No se pudo descargar schema remoto: {e}")
        return []



class IntegrityCheckWorker(QThread):
    """
    Compara el schema.json de GitHub contra la base del cliente.
    Reporta tablas faltantes, campos faltantes y tipos distintos.
    Opcionalmente aplica los fixes (CREATE TABLE / ADD COLUMN / MODIFY COLUMN).
    """
    log      = pyqtSignal(str)
    finished = pyqtSignal(bool, str, list)  # ok, resumen, lista de fixes SQL

    def __init__(self, config: dict, apply_fixes: bool = False, fix_sqls: list = None):
        super().__init__()
        self.config      = config
        self.apply_fixes = apply_fixes
        self.fix_sqls    = fix_sqls or []

    def run(self):
        cfg = self.config
        db  = cfg.get("database", "")

        # Descargar schema
        schema = _fetch_remote_schema()
        if not schema:
            self.finished.emit(False, "No se pudo descargar schema.json de GitHub.", [])
            return

        # Conectar
        try:
            import mysql.connector
            conn = None
            for auth in [None, "mysql_native_password", "caching_sha2_password"]:
                try:
                    kw = dict(host=cfg["host"], port=int(cfg["port"]),
                              user=cfg["user"], password=cfg["password"],
                              connection_timeout=10, use_pure=True)
                    if auth: kw["auth_plugin"] = auth
                    conn = mysql.connector.connect(**kw); break
                except Exception: continue
            if not conn:
                self.finished.emit(False, "No se pudo conectar.", [])
                return
            cur = conn.cursor()
            cur.execute(f"USE `{db}`")
        except Exception as e:
            self.finished.emit(False, f"Error de conexión: {e}", [])
            return

        import re

        # Obtener tablas existentes
        cur.execute("SHOW TABLES")
        tablas_existentes = {r[0].lower() for r in cur.fetchall()}

        fixes = []          # SQLs para corregir
        errores_total = 0
        ok_total      = 0

        self.log.emit(f"📋  Base: '{db}'")
        self.log.emit(f"📐  Schema: {len(schema)} tablas\n")

        for entry in schema:
            tabla = entry["tabla"]
            sql   = entry["sql"]

            if tabla.lower() not in tablas_existentes:
                self.log.emit(f"❌  {tabla}  — TABLA FALTANTE")
                # Fix: CREATE TABLE IF NOT EXISTS (ya está en el sql del schema)
                fixes.append({"tipo": "tabla", "tabla": tabla, "sql": sql})
                errores_total += 1
                continue

            # Tabla existe — verificar columnas
            cur.execute(f"SHOW COLUMNS FROM `{tabla}`")
            cols_db = {}
            for row in cur.fetchall():
                col_name = row[0].lower()
                col_type = row[1].lower()  # ej: "varchar(25)", "decimal(10,4)"
                cols_db[col_name] = col_type

            # Parsear columnas del schema SQL
            cols_schema = {}
            # Buscar líneas tipo: `campo` tipo ...
            for m in re.finditer(
                r'`(\w+)`\s+((?:varchar|decimal|int|bigint|tinyint|datetime|date|longtext|char|text)'
                r'(?:\(\d+(?:,\d+)?\))?)',
                sql, re.IGNORECASE
            ):
                col = m.group(1).lower()
                tip = m.group(2).lower()
                # Ignorar campos del sistema
                if col.startswith('_') or col in ('primary',):
                    continue
                cols_schema[col] = tip

            tabla_ok    = True
            tabla_fixes = []

            for col, tipo_schema in cols_schema.items():
                if col not in cols_db:
                    self.log.emit(
                        f"  ⚠️  {tabla}.{col}  — CAMPO FALTANTE  (schema: {tipo_schema})")
                    # Fix: ADD COLUMN
                    add_sql = (f"ALTER TABLE `{tabla}` ADD COLUMN `{col}` "
                               f"{tipo_schema.upper()} NULL")
                    tabla_fixes.append({"tipo": "campo", "tabla": tabla,
                                        "campo": col, "sql": add_sql})
                    tabla_ok = False
                    errores_total += 1
                else:
                    # Comparar tipo (simplificado — ignorar unsigned, zerofill, etc.)
                    tipo_db = re.sub(r'\s+.*', '', cols_db[col])  # solo "varchar(25)"
                    tipo_sc = re.sub(r'\s+.*', '', tipo_schema)
                    if tipo_db != tipo_sc:
                        # Para VARCHAR: si la base ya es más amplia, no achicar
                        skip = False
                        try:
                            m_db = re.match(r'varchar\((\d+)\)', tipo_db)
                            m_sc = re.match(r'varchar\((\d+)\)', tipo_sc)
                            if m_db and m_sc and int(m_db.group(1)) > int(m_sc.group(1)):
                                skip = True  # base más amplia — OK, no corregir
                        except Exception:
                            pass
                        if skip:
                            self.log.emit(
                                f"  ✅  {tabla}.{col}  — base más amplia que schema "
                                f"({tipo_db} > {tipo_sc}) — OK")
                            continue
                        self.log.emit(
                            f"  ⚡  {tabla}.{col}  — TIPO DISTINTO  "
                            f"(base: {tipo_db}  |  schema: {tipo_sc})")
                        mod_sql = (f"ALTER TABLE `{tabla}` MODIFY COLUMN `{col}` "
                                   f"{tipo_schema.upper()}")
                        tabla_fixes.append({"tipo": "tipo", "tabla": tabla,
                                            "campo": col, "sql": mod_sql})
                        tabla_ok = False
                        errores_total += 1

            if tabla_ok:
                self.log.emit(f"✅  {tabla}")
                ok_total += 1
            else:
                fixes.extend(tabla_fixes)

        self.log.emit(f"\n{'─'*50}")
        self.log.emit(f"✅  {ok_total} tabla(s) OK")
        self.log.emit(f"{'⚠️' if errores_total else '✅'}  {errores_total} problema(s) encontrado(s)")

        # Aplicar fixes si se pidió
        if self.apply_fixes and self.fix_sqls:
            self.log.emit(f"\n🔧  Aplicando {len(self.fix_sqls)} corrección(es)...")
            aplicados = 0
            for sql_fix in self.fix_sqls:
                try:
                    cur.execute(sql_fix)
                    conn.commit()
                    self.log.emit(f"  ✅ {sql_fix[:80]}")
                    aplicados += 1
                except Exception as e:
                    conn.rollback()
                    self.log.emit(f"  ❌ ERROR: {e}")
            self.log.emit(f"\n✅  {aplicados}/{len(self.fix_sqls)} correcciones aplicadas.")

        cur.close(); conn.close()

        resumen = (f"✅ Sin problemas — {ok_total} tablas OK" if errores_total == 0
                   else f"⚠️  {errores_total} problema(s) en {len(schema) - ok_total} tabla(s)")
        self.finished.emit(errores_total == 0, resumen, fixes)


class SchemaUpdaterWorker(QThread):
    """Aplica migraciones pendientes sobre una base existente."""
    log      = pyqtSignal(str)
    finished = pyqtSignal(bool, str)

    def __init__(self, config: dict):
        super().__init__()
        self.config = config

    def _get_connection(self, dbname=None):
        cfg = self.config
        import mysql.connector
        ultimo_error = None
        for auth in [None, "mysql_native_password", "caching_sha2_password"]:
            try:
                kw = dict(host=cfg["host"], port=int(cfg["port"]),
                          user=cfg["user"], password=cfg["password"],
                          connection_timeout=10, use_pure=True)
                if dbname:
                    kw["database"] = dbname
                if auth:
                    kw["auth_plugin"] = auth
                return mysql.connector.connect(**kw)
            except Exception as e:
                ultimo_error = e
        raise ultimo_error

    def _ensure_version_table(self, cur, conn):
        cur.execute("""
            CREATE TABLE IF NOT EXISTS `_schema_version` (
                `version`      INT          NOT NULL,
                `descripcion`  VARCHAR(200) NOT NULL DEFAULT '',
                `aplicado_en`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (`version`)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """)
        conn.commit()

    def _get_current_version(self, cur) -> int:
        try:
            cur.execute("SELECT MAX(version) FROM `_schema_version`")
            row = cur.fetchone()
            return row[0] if row and row[0] is not None else 0
        except Exception as e:
            # Devolver 0 hace que el migrador crea que la base esta virgen y
            # reaplique TODAS las migraciones. Hoy eso es inofensivo porque son
            # idempotentes, pero si no se avisa nadie entiende por que de golpe
            # corrieron 44 migraciones en una base que estaba al dia.
            log = getattr(self, "log", None)
            if log is not None:
                log.emit(f"  ⚠️ No pude leer _schema_version ({e}).")
                log.emit(f"     Voy a asumir version 0 y reaplicar todo.")
            return 0

    def run(self):
        cfg = self.config
        db  = cfg.get("database", "")
        if not db:
            self.finished.emit(False, "No hay base de datos configurada.")
            return

        try:
            conn = self._get_connection()
            cur  = conn.cursor()
            cur.execute("SHOW DATABASES LIKE %s", (db,))
            if not cur.fetchone():
                self.finished.emit(False,
                    f"La base '{db}' no existe en el servidor.\n"
                    "Ejecutá primero la inicialización de tablas.")
                cur.close(); conn.close()
                return
            cur.execute(f"USE `{db}`")
        except Exception as e:
            self.finished.emit(False, f"No se pudo conectar:\n{e}")
            return

        try:
            self._ensure_version_table(cur, conn)
            version_actual = self._get_current_version(cur)
            fuente = '🌐 GitHub' if _MIGRATIONS_SOURCE == 'remoto' else '💾 local'
            self.log.emit(f"📋  Base: '{db}'")
            self.log.emit(f"📌  Versión actual:  {version_actual}")
            self.log.emit(f"🆕  Versión latest:  {SCHEMA_VERSION_LATEST}  ({fuente})")

            pendientes = [m for m in SCHEMA_MIGRATIONS if m["version"] > version_actual]

            if not pendientes:
                self.log.emit("✅  La base ya está en la versión más reciente.")
                self.finished.emit(True, "✅ Sin cambios pendientes — base actualizada.")
                return

            self.log.emit(f"\n⏳  {len(pendientes)} migración(es) pendiente(s):\n")
            errores = []
            for m in pendientes:
                self.log.emit(f"  → v{m['version']}: {m['descripcion']}")
                for sql in m["sql"]:
                    try:
                        cur.execute(sql)
                        conn.commit()
                        self.log.emit(f"     ✅ {sql[:80]}")
                    except Exception as e:
                        err = str(e)
                        # Estos codigos significan "ya estaba aplicado", no error.
                        # Son los que hacen que una migracion se pueda correr dos
                        # veces sin romper nada:
                        #   1146 = la tabla no existe en esta instalacion
                        #   1060 = la columna ya existe (ADD COLUMN repetido)
                        #   1054 = la columna no existe (MODIFY de algo que no esta)
                        #   1061 = el indice ya existe (CREATE INDEX repetido)
                        if "1146" in err or "doesn't exist" in err.lower():
                            self.log.emit(f"     ⏭  Tabla no existe — omitido")
                        elif "1060" in err or "duplicate column" in err.lower():
                            self.log.emit(f"     ⏭  Columna ya existe — omitido")
                        elif "1054" in err:
                            self.log.emit(f"     ⏭  Columna no existe — omitido")
                        elif "1061" in err or "duplicate key name" in err.lower():
                            self.log.emit(f"     ⏭  Indice ya existe — omitido")
                        else:
                            conn.rollback()
                            self.log.emit(f"     ❌ ERROR: {err}")
                            errores.append(f"v{m['version']} — {sql[:60]}: {err}")

                cur.execute(
                    "INSERT IGNORE INTO `_schema_version` (version, descripcion) VALUES (%s, %s)",
                    (m["version"], m["descripcion"])
                )
                conn.commit()

            if errores:
                self.finished.emit(False,
                    f"⚠️  Migraciones aplicadas con {len(errores)} error(es):\n\n"
                    + "\n".join(errores))
            else:
                self.finished.emit(True,
                    f"✅  {len(pendientes)} migración(es) aplicada(s) correctamente.\n"
                    f"Base ahora en versión {SCHEMA_VERSION_LATEST}.")

        except Exception as e:
            self.finished.emit(False, f"Error inesperado:\n{e}")
        finally:
            try:
                cur.close(); conn.close()
            except Exception:
                pass


class SchemaInitWorker(QThread):
    """Crea todas las tablas desde el schema.json remoto (bases nuevas)."""
    log      = pyqtSignal(str)
    finished = pyqtSignal(bool, str)

    def __init__(self, config: dict):
        super().__init__()
        self.config = config

    def run(self):
        cfg = self.config
        db  = cfg.get("database", "")
        if not db:
            self.finished.emit(False, "No hay base de datos configurada.")
            return

        self.log.emit("⬇️  Descargando schema desde GitHub...")
        schema = _fetch_remote_schema()
        if not schema:
            self.finished.emit(False,
                "No se pudo descargar el schema desde GitHub.\n"
                "Verificá la conexión a internet.")
            return

        self.log.emit(f"✅  Schema descargado — {len(schema)} tablas\n")

        try:
            # Conectar sin base
            import mysql.connector
            conn = None
            ultimo_error = None
            for auth in [None, "mysql_native_password", "caching_sha2_password"]:
                try:
                    kw = dict(host=cfg["host"], port=int(cfg["port"]),
                              user=cfg["user"], password=cfg["password"],
                              connection_timeout=10, use_pure=True)
                    if auth: kw["auth_plugin"] = auth
                    conn = mysql.connector.connect(**kw); break
                except Exception as e:
                    ultimo_error = e
            if conn is None:
                raise ultimo_error

            cur = conn.cursor()

            # Crear base si no existe
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{db}` "
                f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            conn.commit()
            cur.execute(f"USE `{db}`")
            self.log.emit(f"📋  Base: '{db}'\n")

            errores = []
            creadas = 0
            existentes = 0

            for entry in schema:
                tabla = entry.get("tabla", "?")
                sql   = entry.get("sql", "")
                if not sql:
                    continue
                try:
                    cur.execute(sql)
                    conn.commit()
                    # Verificar si ya existía
                    cur.execute(f"SELECT COUNT(*) FROM information_schema.tables "
                                f"WHERE table_schema=%s AND table_name=%s", (db, tabla))
                    creadas += 1
                    self.log.emit(f"  ✅  {tabla}")
                except Exception as e:
                    err = str(e)
                    if "already exists" in err.lower():
                        existentes += 1
                        self.log.emit(f"  ⏭   {tabla} (ya existe)")
                    else:
                        errores.append(f"{tabla}: {err}")
                        self.log.emit(f"  ❌  {tabla} — {err}")

            self.log.emit(f"\n📊  Resultado: {creadas} tabla(s) creada(s), "
                          f"{existentes} ya existían, {len(errores)} error(es)")

            cur.close(); conn.close()

            if errores:
                self.finished.emit(False,
                    f"⚠️  Schema aplicado con {len(errores)} error(es):\n\n"
                    + "\n".join(errores))
            else:
                self.finished.emit(True,
                    f"✅  {creadas} tabla(s) creada(s) correctamente.\n"
                    f"{existentes} tablas ya existían — no fueron modificadas.")

        except Exception as e:
            self.finished.emit(False, f"Error inesperado:\n{e}")



    log      = pyqtSignal(str)
    finished = pyqtSignal(bool, str)

    def __init__(self, config: dict):
        super().__init__()
        self.config = config

    def _get_connection(self, dbname=None):
        cfg = self.config
        import mysql.connector
        ultimo_error = None
        for auth in [None, "mysql_native_password", "caching_sha2_password"]:
            try:
                kw = dict(host=cfg["host"], port=int(cfg["port"]),
                          user=cfg["user"], password=cfg["password"],
                          connection_timeout=10, use_pure=True)
                if dbname:
                    kw["database"] = dbname
                if auth:
                    kw["auth_plugin"] = auth
                return mysql.connector.connect(**kw)
            except Exception as e:
                ultimo_error = e
                continue
        raise ultimo_error

    def _ensure_version_table(self, cur, conn):
        cur.execute("""
            CREATE TABLE IF NOT EXISTS `_schema_version` (
                `version`      INT          NOT NULL,
                `descripcion`  VARCHAR(200) NOT NULL DEFAULT '',
                `aplicado_en`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (`version`)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """)
        conn.commit()

    def _get_current_version(self, cur) -> int:
        try:
            cur.execute("SELECT MAX(version) FROM `_schema_version`")
            row = cur.fetchone()
            return row[0] if row and row[0] is not None else 0
        except Exception as e:
            # Devolver 0 hace que el migrador crea que la base esta virgen y
            # reaplique TODAS las migraciones. Hoy eso es inofensivo porque son
            # idempotentes, pero si no se avisa nadie entiende por que de golpe
            # corrieron 44 migraciones en una base que estaba al dia.
            log = getattr(self, "log", None)
            if log is not None:
                log.emit(f"  ⚠️ No pude leer _schema_version ({e}).")
                log.emit(f"     Voy a asumir version 0 y reaplicar todo.")
            return 0

    def run(self):
        cfg = self.config
        db  = cfg.get("database", "")
        if not db:
            self.finished.emit(False, "No hay base de datos configurada.")
            return

        try:
            conn = self._get_connection()  # sin base — igual que test_connection
            cur  = conn.cursor()
            # Verificar que la base existe
            cur.execute("SHOW DATABASES LIKE %s", (db,))
            if not cur.fetchone():
                self.finished.emit(False,
                    f"La base '{db}' no existe en el servidor.\n"
                    "Ejecutá primero la migración de DBF para crearla.")
                cur.close(); conn.close()
                return
            cur.execute(f"USE `{db}`")
        except Exception as e:
            self.finished.emit(False, f"No se pudo conectar:\n{e}")
            return

        try:
            self._ensure_version_table(cur, conn)
            version_actual = self._get_current_version(cur)
            self.log.emit(f"📋  Base: '{db}'")
            self.log.emit(f"📌  Versión actual:  {version_actual}")
            self.log.emit(f"🆕  Versión latest:  {SCHEMA_VERSION_LATEST}  ({'🌐 GitHub' if _MIGRATIONS_SOURCE == 'remoto' else '💾 local — sin internet'})")

            pendientes = [m for m in SCHEMA_MIGRATIONS if m["version"] > version_actual]

            if not pendientes:
                self.log.emit("✅  La base ya está en la versión más reciente.")
                self.finished.emit(True, "✅ Sin cambios pendientes — base actualizada.")
                return

            self.log.emit(f"\n⏳  {len(pendientes)} migración(es) pendiente(s):\n")
            errores = []
            for m in pendientes:
                self.log.emit(f"  → v{m['version']}: {m['descripcion']}")
                for sql in m["sql"]:
                    try:
                        # Ignorar si la tabla no existe (cliente sin ese módulo)
                        cur.execute(sql)
                        conn.commit()
                        self.log.emit(f"     ✅ {sql[:80]}")
                    except Exception as e:
                        err = str(e)
                        # Estos codigos significan "ya estaba aplicado", no error.
                        # Son los que hacen que una migracion se pueda correr dos
                        # veces sin romper nada:
                        #   1146 = la tabla no existe en esta instalacion
                        #   1060 = la columna ya existe (ADD COLUMN repetido)
                        #   1054 = la columna no existe (MODIFY de algo que no esta)
                        #   1061 = el indice ya existe (CREATE INDEX repetido)
                        if "1146" in err or "doesn't exist" in err.lower():
                            self.log.emit(f"     ⏭  Tabla no existe — omitido")
                        elif "1060" in err or "duplicate column" in err.lower():
                            self.log.emit(f"     ⏭  Columna ya existe — omitido")
                        elif "1054" in err:
                            self.log.emit(f"     ⏭  Columna no existe — omitido")
                        elif "1061" in err or "duplicate key name" in err.lower():
                            self.log.emit(f"     ⏭  Indice ya existe — omitido")
                        else:
                            conn.rollback()
                            self.log.emit(f"     ❌ ERROR: {err}")
                            errores.append(f"v{m['version']} — {sql[:60]}: {err}")

                # Registrar versión aunque haya errores parciales
                cur.execute(
                    "INSERT IGNORE INTO `_schema_version` (version, descripcion) VALUES (%s, %s)",
                    (m["version"], m["descripcion"])
                )
                conn.commit()

            if errores:
                self.finished.emit(False,
                    f"⚠️  Migraciones aplicadas con {len(errores)} error(es):\n\n"
                    + "\n".join(errores))
            else:
                self.finished.emit(True,
                    f"✅  {len(pendientes)} migración(es) aplicada(s) correctamente.\n"
                    f"Base ahora en versión {SCHEMA_VERSION_LATEST}.")

        except Exception as e:
            self.finished.emit(False, f"Error inesperado:\n{traceback.format_exc()}")
        finally:
            try:
                cur.close(); conn.close()
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════
#  ESTILOS
# ══════════════════════════════════════════════════════════════════

STYLE = """
QMainWindow, QWidget { background-color:#0f1117; color:#e2e8f0;
    font-family:'Segoe UI','SF Pro Display',system-ui,sans-serif; font-size:13px; }
QGroupBox { border:1px solid #2d3748; border-radius:8px; margin-top:12px;
    padding:12px 10px 10px 10px; font-weight:600; font-size:11px;
    letter-spacing:1px; color:#7c8db5; }
QGroupBox::title { subcontrol-origin:margin; left:12px; top:-7px;
    padding:0 6px; background-color:#0f1117; }
QLineEdit, QSpinBox, QComboBox { background-color:#1a202c; border:1px solid #2d3748;
    border-radius:6px; padding:6px 10px; color:#e2e8f0; }
QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border-color:#3b82f6; }
QComboBox::drop-down { border:none; width:24px; }
QComboBox QAbstractItemView { background-color:#1a202c; border:1px solid #2d3748;
    selection-background-color:#2563eb; outline:none; }

QPushButton#btn_primary {
    background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #2563eb,stop:1 #7c3aed);
    color:white; border:none; border-radius:8px; padding:10px 24px;
    font-weight:700; font-size:13px; }
QPushButton#btn_primary:hover {
    background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #1d4ed8,stop:1 #6d28d9); }
QPushButton#btn_primary:disabled { background:#374151; color:#6b7280; }

QPushButton#btn_test {
    background-color:#064e3b; border:1px solid #065f46;
    border-radius:8px; padding:10px 24px; color:#6ee7b7;
    font-weight:700; font-size:13px; }
QPushButton#btn_test:hover { background-color:#065f46; }
QPushButton#btn_test:disabled { background:#1f2937; color:#4b5563; }

QPushButton#btn_inject {
    background-color:#1e3a5f; border:1px solid #2563eb;
    border-radius:8px; padding:10px 24px; color:#93c5fd;
    font-weight:700; font-size:13px; }
QPushButton#btn_inject:hover { background-color:#1d4ed8; color:#fff; }
QPushButton#btn_inject:disabled { background:#1f2937; color:#4b5563; }

QPushButton#btn_procs {
    background-color:#2d1f4e; border:1px solid #7c3aed;
    border-radius:8px; padding:10px 24px; color:#c4b5fd;
    font-weight:700; font-size:13px; }
QPushButton#btn_procs:hover { background-color:#5b21b6; color:#fff; }
QPushButton#btn_procs:disabled { background:#1f2937; color:#4b5563; }

QPushButton { background-color:#1e293b; border:1px solid #334155;
    border-radius:6px; padding:7px 16px; color:#cbd5e1; font-weight:500; }
QPushButton:hover { background-color:#273548; border-color:#4b6080; }

QListWidget { background-color:#131924; border:1px solid #2d3748;
    border-radius:8px; padding:4px; outline:none; }
QListWidget::item { padding:8px 12px; border-radius:5px; margin:2px; color:#94a3b8; }
QListWidget::item:selected { background-color:#1e3a5f; color:#e2e8f0; }
QListWidget::item:hover { background-color:#1a2535; }

QTextEdit { background-color:#0a0e17; border:1px solid #1e293b; border-radius:8px;
    font-family:'Cascadia Code','Consolas','Fira Code',monospace;
    font-size:12px; color:#7dd3fc; padding:8px; }

QProgressBar { border:none; border-radius:5px; background-color:#1e293b;
    height:10px; text-align:center; color:transparent; }
QProgressBar::chunk { background:qlineargradient(x1:0,y1:0,x2:1,y2:0,
    stop:0 #2563eb,stop:1 #7c3aed); border-radius:5px; }

QCheckBox { spacing:8px; }
QCheckBox::indicator { width:16px; height:16px; border-radius:4px;
    border:1px solid #4b5563; background-color:#1a202c; }
QCheckBox::indicator:checked { background-color:#2563eb; border-color:#2563eb; }

QFrame[frameShape="4"], QFrame[frameShape="5"] { color:#1e293b; }

QTabWidget::pane { border:1px solid #2d3748; border-radius:8px; background-color:#0f1117; }
QTabBar::tab { background:#131924; border:1px solid #2d3748; border-bottom:none;
    padding:8px 18px; border-top-left-radius:6px; border-top-right-radius:6px;
    color:#64748b; font-weight:600; font-size:12px; }
QTabBar::tab:selected { background:#1a2535; color:#e2e8f0; }
QTabBar::tab:hover { background:#1a2535; color:#94a3b8; }

QLabel#title_label { font-size:22px; font-weight:800; color:#f1f5f9; }
QLabel#subtitle_label { font-size:12px; color:#64748b; }
QLabel#badge { background-color:#1e3a5f; color:#60a5fa; border-radius:10px;
    padding:2px 10px; font-size:11px; font-weight:600; }
QLabel#conn_status { font-size:12px; font-weight:600; padding:6px 12px;
    border-radius:6px; }
"""


# ══════════════════════════════════════════════════════════════════
#  VENTANA PRINCIPAL
# ══════════════════════════════════════════════════════════════════

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"DBF Migrator  v{APP_VERSION}")
        self.setMinimumSize(960, 700)
        self.resize(1100, 780)
        self.worker      = None
        self.test_worker = None
        self._conn_ok    = False

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        # ── Header ───────────────────────────────────────────────
        hdr = QWidget(); hl = QHBoxLayout(hdr); hl.setContentsMargins(0,0,0,0)
        tb  = QVBoxLayout()
        t   = QLabel("DBF Migrator"); t.setObjectName("title_label")
        s   = QLabel("DBF (VFP)  →  MySQL / PostgreSQL  •  Sync bidireccional con API")
        s.setObjectName("subtitle_label")
        tb.addWidget(t); tb.addWidget(s)
        hl.addLayout(tb); hl.addStretch()
        b = QLabel(f"v{APP_VERSION}  •  15 campos de control"); b.setObjectName("badge")
        hl.addWidget(b)
        root.addWidget(hdr)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        root.addWidget(sep)

        # ── Splitter ─────────────────────────────────────────────
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(1)
        splitter.addWidget(self._left_panel())
        splitter.addWidget(self._right_panel())
        splitter.setSizes([400, 660])
        root.addWidget(splitter, 1)

        # ── Barra de progreso ────────────────────────────────────
        self.progress_bar = QProgressBar(); self.progress_bar.setValue(0)
        root.addWidget(self.progress_bar)

        # ── Botones principales ──────────────────────────────────
        btn_row = QHBoxLayout(); btn_row.setSpacing(10)

        self.btn_test = QPushButton("🔌  Verificar Conexión")
        self.btn_test.setObjectName("btn_test")
        self.btn_test.setMinimumHeight(44)
        self.btn_test.clicked.connect(self.test_connection)

        self.btn_migrate = QPushButton("⚡  Iniciar Migración")
        self.btn_migrate.setObjectName("btn_primary")
        self.btn_migrate.setMinimumHeight(44)
        self.btn_migrate.clicked.connect(self.start_migration)

        self.btn_inject = QPushButton("🗂  Inyectar Tablas")
        self.btn_inject.setObjectName("btn_inject")
        self.btn_inject.setMinimumHeight(44)
        self.btn_inject.setToolTip("Crea las tablas auxiliares si no existen (no borra datos)")
        self.btn_inject.clicked.connect(self.inject_tables)

        self.btn_procs = QPushButton("⚙  Inyectar Procedures")
        self.btn_procs.setObjectName("btn_procs")
        self.btn_procs.setMinimumHeight(44)
        self.btn_procs.setToolTip("Crea los stored procedures si no existen")
        self.btn_procs.clicked.connect(self.inject_procedures)

        btn_row.addWidget(self.btn_test, 1)
        btn_row.addWidget(self.btn_inject, 1)
        btn_row.addWidget(self.btn_procs, 1)
        btn_row.addWidget(self.btn_migrate, 2)
        root.addLayout(btn_row)

        # ── Estado de conexión ───────────────────────────────────
        self.lbl_conn = QLabel("⚪  Sin verificar — probá la conexión antes de migrar")
        self.lbl_conn.setObjectName("conn_status")
        self.lbl_conn.setStyleSheet(
            "QLabel { background-color:#1e293b; color:#94a3b8; "
            "border-radius:6px; padding:6px 12px; font-size:12px; font-weight:600; }")
        self.lbl_conn.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.lbl_conn)

        # ── Cargar config guardada ───────────────────────────────
        self._load_saved_config()

    # ── Persistencia de configuración ────────────────────────────
    def _load_saved_config(self):
        """Carga la última configuración usada al iniciar."""
        cfg = load_saved_config()
        # Motor
        idx = self.cmb_db.findText(cfg.get("db_type", "MySQL"))
        if idx >= 0:
            self.cmb_db.setCurrentIndex(idx)
        # Conexión
        self.txt_host.setText(cfg.get("host", "localhost"))
        self.txt_port.setValue(int(cfg.get("port", 3306)))
        self.txt_user.setText(cfg.get("user", "root"))
        self.txt_db.setText(cfg.get("database", "mi_base_dbf"))
        # Opciones
        enc_idx = self.cmb_enc.findText(cfg.get("encoding", "latin-1"))
        if enc_idx >= 0:
            self.cmb_enc.setCurrentIndex(enc_idx)
        self.chk_drop.setChecked(cfg.get("drop_if_exists", False))
        # Nota: contraseña NO se carga por seguridad

        # Cargar archivos DBF guardados
        for f in load_saved_files():
            item = QListWidgetItem(f"  {Path(f).name}")
            item.setData(Qt.ItemDataRole.UserRole, f)
            item.setToolTip(f)
            self.file_list.addItem(item)
        self._upd_count()

        if cfg.get("host") != "localhost" or cfg.get("database") != "mi_base_dbf":
            self.lbl_conn.setText(
                "⚪  Configuración cargada — verificá la conexión para confirmar")

    def _save_current_config(self):
        """Guarda la configuración actual (sin contraseña)."""
        save_config(self._get_config())

    # ── Panel izquierdo ───────────────────────────────────────────
    def _left_panel(self):
        w  = QWidget(); vl = QVBoxLayout(w)
        vl.setContentsMargins(0,0,6,0); vl.setSpacing(8)

        grp = QGroupBox("Archivos DBF"); gl = QVBoxLayout(grp)
        br  = QHBoxLayout()
        for label, slot in [("➕ Agregar", self.add_files),
                             ("✖ Quitar",  self.remove_selected),
                             ("🗑️ Limpiar", self.clear_files)]:
            b = QPushButton(label); b.clicked.connect(slot); br.addWidget(b)
        gl.addLayout(br)

        # Botón sugerir desde schema
        btn_suggest = QPushButton("🔍  Sugerir desde schema")
        btn_suggest.setToolTip(
            "Compara el schema.json de GitHub con los DBF de una carpeta\n"
            "y agrega automáticamente los que encuentra.\n\n"
            "Además avisa si el ERP escribe en alguna tabla que el schema\n"
            "no tiene: esas nunca se crean y sus INSERT fallan con 1146.")
        btn_suggest.clicked.connect(self._suggest_from_schema)
        gl.addWidget(btn_suggest)

        self.file_list = QListWidget()
        self.file_list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.file_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        gl.addWidget(self.file_list)

        self.lbl_count = QLabel("Sin archivos seleccionados")
        self.lbl_count.setObjectName("subtitle_label")
        self.lbl_count.setAlignment(Qt.AlignmentFlag.AlignCenter)
        gl.addWidget(self.lbl_count)
        vl.addWidget(grp, 1)

        # Campos de control
        grp2 = QGroupBox("Campos de control  (al final de cada tabla)")
        g2l  = QVBoxLayout(grp2); g2l.setSpacing(3)
        for campo, desc in CAMPOS_INFO:
            row = QHBoxLayout(); row.setContentsMargins(0,0,0,0)
            lc  = QLabel(campo)
            lc.setStyleSheet("color:#60a5fa;font-family:monospace;font-size:11px;")
            lc.setFixedWidth(150)
            ld  = QLabel(desc)
            ld.setStyleSheet("color:#64748b;font-size:10px;")
            row.addWidget(lc); row.addWidget(ld); row.addStretch()
            g2l.addLayout(row)
        vl.addWidget(grp2)
        return w

    # ── Panel derecho ─────────────────────────────────────────────
    def _right_panel(self):
        w  = QWidget(); vl = QVBoxLayout(w)
        vl.setContentsMargins(6,0,0,0); vl.setSpacing(8)
        tabs = QTabWidget()

        # Tab Conexión
        ct = QWidget(); cl = QVBoxLayout(ct); cl.setSpacing(10)

        grp_db = QGroupBox("Motor"); dbf = QFormLayout(grp_db)
        self.cmb_db = QComboBox(); self.cmb_db.addItems(["MySQL","PostgreSQL"])
        self.cmb_db.currentTextChanged.connect(self._on_db_change)
        dbf.addRow("Motor:", self.cmb_db)
        cl.addWidget(grp_db)

        grp_cn = QGroupBox("Parámetros de conexión"); cf = QFormLayout(grp_cn)
        self.txt_host = QLineEdit("localhost")
        self.txt_port = QSpinBox(); self.txt_port.setRange(1,65535); self.txt_port.setValue(3306)
        self.txt_user = QLineEdit("root")
        self.txt_pass = QLineEdit(); self.txt_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self.txt_db   = QLineEdit("mi_base_dbf")

        for w2 in [self.txt_host, self.txt_user, self.txt_pass, self.txt_db]:
            w2.textChanged.connect(self._reset_conn_status)
        self.txt_port.valueChanged.connect(self._reset_conn_status)

        for label, widget in [("Host:", self.txt_host), ("Puerto:", self.txt_port),
                               ("Usuario:", self.txt_user), ("Contraseña:", self.txt_pass),
                               ("Base de datos:", self.txt_db)]:
            cf.addRow(label, widget)
        cl.addWidget(grp_cn)

        grp_op = QGroupBox("Opciones"); of = QFormLayout(grp_op)
        self.cmb_enc = QComboBox()
        self.cmb_enc.addItems(["latin-1","cp850","cp1252","utf-8","iso-8859-1"])
        self.chk_drop = QCheckBox("Eliminar tabla si ya existe (DROP + CREATE)")
        of.addRow("Encoding DBF:", self.cmb_enc)
        of.addRow(self.chk_drop)
        cl.addWidget(grp_op); cl.addStretch()
        tabs.addTab(ct, "⚙️  Conexión")

        # Tab Log
        lt = QWidget(); ll = QVBoxLayout(lt)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setPlaceholderText("Los mensajes aparecerán aquí…")
        ll.addWidget(self.log_view)
        bc = QPushButton("Limpiar log"); bc.clicked.connect(self.log_view.clear)
        ll.addWidget(bc)
        tabs.addTab(lt, "📋  Log")

        # Tab Resultados
        rt = QWidget(); rl = QVBoxLayout(rt)
        self.res_list = QListWidget()
        rl.addWidget(self.res_list)
        tabs.addTab(rt, "✅  Resultados")

        vl.addWidget(tabs, 1)

        # ── Tab Inicializar Base ──────────────────────────────────
        it = QWidget(); il = QVBoxLayout(it); il.setSpacing(10)

        grp_init = QGroupBox("Inicializar base de datos")
        gi2 = QVBoxLayout(grp_init)
        lbl = QLabel(
            "Crea todas las tablas del sistema en la base configurada.\n"
            "Si una tabla ya existe NO la modifica — es seguro correr en bases existentes.\n"
            "El schema se descarga desde GitHub automáticamente."
        )
        lbl.setWordWrap(True)
        lbl.setStyleSheet("font-size:12px; padding:4px;")
        gi2.addWidget(lbl)
        il.addWidget(grp_init)

        self.btn_init_schema = QPushButton("🏗️  Inicializar / Verificar tablas")
        self.btn_init_schema.clicked.connect(self._run_schema_init)
        il.addWidget(self.btn_init_schema)

        self.lbl_init_status = QLabel("")
        self.lbl_init_status.setWordWrap(True)
        self.lbl_init_status.setStyleSheet("font-size:12px; padding:4px;")
        il.addWidget(self.lbl_init_status)

        self.init_log = QTextEdit()
        self.init_log.setReadOnly(True)
        self.init_log.setPlaceholderText("El resultado aparecerá aquí…")
        il.addWidget(self.init_log, 1)

        tabs.addTab(it, "🏗️  Inicializar Base")

        # ── Tab Migraciones ───────────────────────────────────────
        mt = QWidget(); ml = QVBoxLayout(mt); ml.setSpacing(10)

        grp_info = QGroupBox("Control de versiones del schema")
        gi = QVBoxLayout(grp_info)
        self.lbl_schema_ver = QLabel(
            f"Versión en base:  —\n"
            f"Versión latest:   {SCHEMA_VERSION_LATEST}  "
            f"({'🌐 remoto' if _MIGRATIONS_SOURCE == 'remoto' else '💾 local (sin internet)'})"
        )
        self.lbl_schema_ver.setStyleSheet("font-family:monospace; font-size:12px; padding:4px;")
        gi.addWidget(self.lbl_schema_ver)
        ml.addWidget(grp_info)

        grp_mig = QGroupBox("Migraciones disponibles")
        gm = QVBoxLayout(grp_mig)
        self.mig_list = QTextEdit()
        self.mig_list.setReadOnly(True)
        self.mig_list.setMaximumHeight(180)
        # Mostrar lista de migraciones
        lines = []
        for m in SCHEMA_MIGRATIONS:
            lines.append(f"  v{m['version']}  {m['descripcion']}")
            for s in m["sql"]:
                lines.append(f"         {s[:70]}")
        self.mig_list.setPlainText("\n".join(lines))
        gm.addWidget(self.mig_list)
        ml.addWidget(grp_mig)

        self.btn_check_ver = QPushButton("🔍  Ver versión actual de la base")
        self.btn_check_ver.clicked.connect(self._check_schema_version)
        ml.addWidget(self.btn_check_ver)

        self.btn_run_mig = QPushButton("🚀  Aplicar migraciones pendientes")
        self.btn_run_mig.clicked.connect(self._run_migrations)
        ml.addWidget(self.btn_run_mig)

        self.btn_integrity = QPushButton("🔎  Verificar integridad de tablas y campos")
        self.btn_integrity.clicked.connect(self._run_integrity_check)
        ml.addWidget(self.btn_integrity)

        self.lbl_mig_status = QLabel("")
        self.lbl_mig_status.setWordWrap(True)
        self.lbl_mig_status.setStyleSheet("font-size:12px; padding:4px;")
        ml.addWidget(self.lbl_mig_status)

        self.mig_log = QTextEdit()
        self.mig_log.setReadOnly(True)
        self.mig_log.setPlaceholderText("El log de migraciones aparecerá aquí…")
        ml.addWidget(self.mig_log, 1)
        ml.addStretch()

        tabs.addTab(mt, "🔄  Migraciones")

        return w

    # ── Helpers ──────────────────────────────────────────────────
    def _on_db_change(self, text):
        self.txt_port.setValue(3306 if text == "MySQL" else 5432)
        self._reset_conn_status()

    def _reset_conn_status(self):
        self._conn_ok = False
        self.lbl_conn.setText("⚪  Sin verificar — modificaste los datos, volvé a verificar")
        self.lbl_conn.setStyleSheet(
            "QLabel { background-color:#1e293b; color:#94a3b8; "
            "border-radius:6px; padding:6px 12px; font-size:12px; font-weight:600; }")

    def _get_config(self) -> dict:
        return {
            "db_type":        self.cmb_db.currentText(),
            "host":           self.txt_host.text().strip(),
            "port":           self.txt_port.value(),
            "user":           self.txt_user.text().strip(),
            "password":       self.txt_pass.text(),
            "database":       self.txt_db.text().strip() or "mi_base_dbf",
            "encoding":       self.cmb_enc.currentText(),
            "drop_if_exists": self.chk_drop.isChecked(),
        }

    # ── Archivos ─────────────────────────────────────────────────
    def _suggest_from_schema(self):
        """Descarga schema.json, busca los DBF en una carpeta y sugiere cuáles agregar."""
        # 1 — Elegir carpeta de DBF
        carpeta = QFileDialog.getExistingDirectory(
            self, "Seleccioná la carpeta con los archivos DBF")
        if not carpeta:
            return

        # 2 — Descargar schema
        schema = _fetch_remote_schema()
        if not schema:
            QMessageBox.warning(self, "Sin schema",
                "No se pudo descargar el schema.json desde GitHub.\n"
                "Verificá la conexión a internet.")
            return

        # 3 — Comparar contra la carpeta
        encontrados = []
        no_encontrados = []
        existing_paths = {self.file_list.item(i).data(Qt.ItemDataRole.UserRole)
                          for i in range(self.file_list.count())}

        def _buscar_dbf(tabla):
            """Busca <tabla>.dbf en la carpeta probando mayusculas/minusculas."""
            for nombre in [tabla, tabla.upper(), tabla.lower(), tabla.capitalize()]:
                for ext in (".dbf", ".DBF"):
                    candidato = os.path.join(carpeta, nombre + ext)
                    if os.path.exists(candidato):
                        return candidato
            return None

        for entry in schema:
            tabla = entry["tabla"]
            path = _buscar_dbf(tabla)
            if path:
                encontrados.append((tabla, path))
            else:
                no_encontrados.append(tabla)

        # 3b — Tablas en las que el ERP escribe y que el schema NO tiene.
        # Sin esto el boton no las veia: el migrador nunca las creaba y cada
        # INSERT del ERP moria con 1146. Ver TABLAS_QUE_ESCRIBE_EL_ERP.
        tablas_schema = {e["tabla"].lower() for e in schema}
        huerfanas = []       # (tabla, path o None)
        for tabla in TABLAS_QUE_ESCRIBE_EL_ERP:
            if tabla.lower() in tablas_schema:
                continue
            huerfanas.append((tabla, _buscar_dbf(tabla)))

        if not encontrados and not huerfanas:
            QMessageBox.information(self, "Sin coincidencias",
                f"No se encontró ningún DBF del schema en:\n{carpeta}")
            return

        # 4 — Mostrar diálogo de selección
        dlg = QDialog(self)
        dlg.setWindowTitle("Tablas encontradas — seleccioná cuáles agregar")
        dlg.setMinimumWidth(520)
        dlg_layout = QVBoxLayout(dlg)

        lbl_info = QLabel(
            f"<b>{len(encontrados)}</b> tablas encontradas en la carpeta.  "
            f"<span style='color:#94a3b8;'>{len(no_encontrados)} no encontradas.</span>"
        )
        lbl_info.setWordWrap(True)
        dlg_layout.addWidget(lbl_info)

        # Checkboxes para cada tabla encontrada
        checks = []
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        inner = QWidget(); inner_layout = QVBoxLayout(inner)
        inner_layout.setSpacing(4)

        # ── Primero las huerfanas: el ERP les escribe y el schema no las tiene.
        # Van arriba y marcadas porque son las que hay que agregar: mientras no
        # esten, el migrador no las crea y los INSERT del ERP fallan con 1146.
        if huerfanas:
            lbl_h = QLabel(
                "<b style='color:#f59e0b;'>&#9888; El ERP escribe en estas tablas "
                "y el schema no las tiene</b><br>"
                "<span style='color:#94a3b8;'>Mientras no esten en el schema el "
                "migrador no las crea, y cada INSERT del ERP falla con 1146 "
                "&laquo;Table doesn't exist&raquo;. Agregalas para que el migrador "
                "genere su estructura desde el DBF.</span>")
            lbl_h.setWordWrap(True)
            inner_layout.addWidget(lbl_h)

            for tabla, path in sorted(huerfanas):
                if path:
                    ya_esta = path in existing_paths
                    chk = QCheckBox(f"  {tabla.ljust(20)}  \u2192  {Path(path).name}")
                    chk.setChecked(not ya_esta)
                    chk.setEnabled(not ya_esta)
                    if ya_esta:
                        chk.setText(chk.text() + "  (ya agregada)")
                        chk.setStyleSheet("color:#64748b;")
                    else:
                        chk.setStyleSheet("color:#f59e0b; font-weight:bold;")
                    chk.setProperty("path", path)
                    inner_layout.addWidget(chk)
                    checks.append(chk)
                else:
                    lbl_nf = QLabel(
                        f"<span style='color:#ef4444;'>  {tabla}  &mdash; no se "
                        f"encontro el DBF en esta carpeta</span>")
                    lbl_nf.setWordWrap(True)
                    inner_layout.addWidget(lbl_nf)

            sep_h = QLabel("<hr>")
            inner_layout.addWidget(sep_h)
            lbl_sch = QLabel("<b>Tablas del schema</b>")
            inner_layout.addWidget(lbl_sch)

        for tabla, path in sorted(encontrados):
            ya_esta = path in existing_paths
            chk = QCheckBox(f"  {tabla.ljust(20)}  →  {Path(path).name}")
            chk.setChecked(not ya_esta)
            chk.setEnabled(not ya_esta)
            if ya_esta:
                chk.setText(chk.text() + "  (ya agregada)")
                chk.setStyleSheet("color:#64748b;")
            chk.setProperty("path", path)
            inner_layout.addWidget(chk)
            checks.append(chk)

        if no_encontrados:
            sep = QLabel(f"\n<span style='color:#64748b;'>No encontradas: "
                         f"{', '.join(no_encontrados)}</span>")
            sep.setWordWrap(True)
            inner_layout.addWidget(sep)

        inner_layout.addStretch()
        scroll.setWidget(inner)
        dlg_layout.addWidget(scroll, 1)

        # Botones seleccionar todo / ninguno
        row_sel = QHBoxLayout()
        btn_all  = QPushButton("Seleccionar todas")
        btn_none = QPushButton("Ninguna")
        btn_all.clicked.connect(lambda: [c.setChecked(True)  for c in checks if c.isEnabled()])
        btn_none.clicked.connect(lambda: [c.setChecked(False) for c in checks if c.isEnabled()])
        row_sel.addWidget(btn_all); row_sel.addWidget(btn_none); row_sel.addStretch()
        dlg_layout.addLayout(row_sel)

        # OK / Cancelar
        row_btn = QHBoxLayout()
        btn_ok     = QPushButton("✅  Agregar seleccionadas")
        btn_cancel = QPushButton("Cancelar")
        btn_ok.clicked.connect(dlg.accept)
        btn_cancel.clicked.connect(dlg.reject)
        row_btn.addStretch(); row_btn.addWidget(btn_cancel); row_btn.addWidget(btn_ok)
        dlg_layout.addLayout(row_btn)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        # 5 — Agregar los seleccionados al file_list
        agregadas = 0
        for chk in checks:
            if chk.isChecked() and chk.isEnabled():
                path = chk.property("path")
                if path not in existing_paths:
                    item = QListWidgetItem(f"  {Path(path).name}")
                    item.setData(Qt.ItemDataRole.UserRole, path)
                    item.setToolTip(path)
                    self.file_list.addItem(item)
                    agregadas += 1

        self._upd_count()
        self._guardar_archivos()

        if agregadas:
            QMessageBox.information(self, "Listo",
                f"✅  {agregadas} archivo(s) agregado(s) a la lista.")

    def add_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Seleccionar archivos DBF", "", "DBF (*.dbf *.DBF)")
        existing = {self.file_list.item(i).data(Qt.ItemDataRole.UserRole)
                    for i in range(self.file_list.count())}
        for f in files:
            if f not in existing:
                item = QListWidgetItem(f"  {Path(f).name}")
                item.setData(Qt.ItemDataRole.UserRole, f)
                item.setToolTip(f)
                self.file_list.addItem(item)
        self._upd_count()
        self._guardar_archivos()

    def remove_selected(self):
        for item in self.file_list.selectedItems():
            self.file_list.takeItem(self.file_list.row(item))
        self._upd_count()
        self._guardar_archivos()

    def clear_files(self):
        self.file_list.clear()
        self._upd_count()
        self._guardar_archivos()

    def _guardar_archivos(self):
        """Guarda la lista de archivos y muestra feedback en la UI."""
        ok = save_files(self._files())
        n  = self.file_list.count()
        if ok:
            self.lbl_count.setText(
                f"{'Sin archivos' if n == 0 else f'{n} archivo{chr(115) if n>1 else chr(32)} listo{chr(115) if n>1 else chr(32)}'}"
                f"  •  💾 guardado"
            )
        else:
            self.lbl_count.setText(
                f"{n} archivo(s)  •  ⚠️ no se pudo guardar — revisá permisos en {CONFIG_FILE}"
            )

    def _upd_count(self):
        n = self.file_list.count()
        self.lbl_count.setText(
            "Sin archivos seleccionados" if n == 0
            else f"{n} archivo{'s' if n>1 else ''} listo{'s' if n>1 else ''}")

    def _files(self):
        return [self.file_list.item(i).data(Qt.ItemDataRole.UserRole)
                for i in range(self.file_list.count())]

    # ── Verificar conexión ────────────────────────────────────────
    # ── Tablas auxiliares a inyectar ─────────────────────────────
    TABLAS_AUXILIARES = [
        # ── acl ──
        """CREATE TABLE IF NOT EXISTS `acl` (
  `codigo` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `nombre` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `fantasia` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `direc` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cod_postal` varchar(8) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `locali` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `fax` varchar(25) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `telefono` varchar(60) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cuit` varchar(13) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `pos` varchar(2) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `tipo_pos` decimal(2,0) DEFAULT NULL,
  `tipo_doc` decimal(2,0) DEFAULT NULL,
  `obs` varchar(1) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `provi` varchar(3) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cli_pro` varchar(11) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cod_estado` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cod_zona` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cod_vend` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `clasif` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cl_lista` varchar(1) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `monecc` varchar(3) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `saldo_cc` decimal(12,2) DEFAULT NULL,
  `bloqueo_cc` tinyint(1) DEFAULT NULL,
  `apli_licre` varchar(1) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `limite_cre` decimal(12,2) DEFAULT NULL,
  `impri_eti` tinyint(1) DEFAULT NULL,
  `impri_pa` varchar(1) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `bonif_1` decimal(5,2) DEFAULT NULL,
  `bonif_12` decimal(5,2) DEFAULT NULL,
  `bonif_13` decimal(5,2) DEFAULT NULL,
  `bonif_2` decimal(5,2) DEFAULT NULL,
  `desc_art` varchar(1) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `t_pago` varchar(2) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `f_pago1` decimal(3,0) DEFAULT NULL,
  `f_pago2` decimal(3,0) DEFAULT NULL,
  `f_pago3` decimal(3,0) DEFAULT NULL,
  `f_pago4` decimal(3,0) DEFAULT NULL,
  `f_pago5` decimal(3,0) DEFAULT NULL,
  `f_pago6` decimal(3,0) DEFAULT NULL,
  `cod_trans` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `ulti_ope` date DEFAULT NULL,
  `liberado` decimal(6,2) DEFAULT NULL,
  `iva_exen` tinyint(1) DEFAULT NULL,
  `ib_tipo` varchar(1) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `ib_nro` varchar(11) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `iva_porc` decimal(5,2) DEFAULT NULL,
  `ib_exen` tinyint(1) DEFAULT NULL,
  `ib_porc` decimal(5,2) DEFAULT NULL,
  `ib_exen_901` tinyint(1) DEFAULT NULL,
  `ib_porc_901` decimal(5,2) DEFAULT NULL,
  `ib_exen_924` tinyint(1) DEFAULT NULL,
  `ib_porc_924` decimal(5,2) DEFAULT NULL,
  `ib_exen_914` tinyint(1) DEFAULT NULL,
  `ib_porc_914` decimal(5,2) DEFAULT NULL,
  `ib_exen_93` tinyint(1) DEFAULT NULL,
  `ib_porc_93` decimal(5,2) DEFAULT NULL,
  `cl_obs` varchar(45) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cl_anotador` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci,
  `impri_opcion` varchar(1) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `p_venta` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `f_alta` date DEFAULT NULL,
  `tcompras` decimal(15,2) DEFAULT NULL,
  `modali` varchar(2) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `situa` varchar(2) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `mkfecha` date DEFAULT NULL,
  `mkfechaul` date DEFAULT NULL,
  `lote` decimal(5,0) DEFAULT NULL,
  `bonif_22` decimal(5,2) DEFAULT NULL,
  `bonif_23` decimal(5,2) DEFAULT NULL,
  `bonif_3` decimal(5,2) DEFAULT NULL,
  `fcredito` tinyint(1) DEFAULT NULL,
  `fchange` date DEFAULT NULL,
  `id_acl` varchar(8) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cod_vend2` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `ib_porc_94` decimal(6,2) DEFAULT NULL,
  `ib_exen_94` tinyint(1) DEFAULT NULL,
  `c_min` decimal(15,2) DEFAULT NULL,
  `w_user` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `ib_exen_921` tinyint(1) NOT NULL DEFAULT '0',
  `ib_porc_921` decimal(5,2) NOT NULL DEFAULT '0.00',
  `ib_exen_917` tinyint(1) NOT NULL DEFAULT '0',
  `ib_porc_917` decimal(5,2) NOT NULL DEFAULT '0.00',
  `wasap` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `_id` bigint NOT NULL AUTO_INCREMENT,
  `_uuid` char(36) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT (uuid()),
  `_creado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `_modificado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  `_sincronizado_en` datetime(3) DEFAULT NULL,
  `_sync_estado` tinyint(1) NOT NULL DEFAULT '0',
  `_sync_version` int NOT NULL DEFAULT '1',
  `_origen` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'MIGRADOR',
  `_eliminado` tinyint(1) NOT NULL DEFAULT '0',
  `_eliminado_en` datetime(3) DEFAULT NULL,
  `_hash` char(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `_api_id` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `update_at` datetime DEFAULT NULL,
  `syncro_at` datetime DEFAULT NULL,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

        # ── acl2 ──
        """CREATE TABLE IF NOT EXISTS `acl2` (
  `_id` bigint NOT NULL AUTO_INCREMENT,
  `codigo` varchar(4) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `campo` varchar(2) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `contenido` varchar(120) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `delete_at` datetime DEFAULT NULL,
  `update_at` datetime DEFAULT NULL,
  `syncro_at` datetime DEFAULT NULL,
  `deleted_at` datetime DEFAULT NULL,
  `_sync_estado` tinyint(1) NOT NULL DEFAULT '0',
  `_eliminado` tinyint(1) NOT NULL DEFAULT '0',
  `_eliminado_en` datetime(3) DEFAULT NULL,
  `_uuid` char(36) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT (uuid()),
  `_creado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `_modificado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  `_origen` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'MIGRADOR',
  `_sync_version` int NOT NULL DEFAULT '1',
  PRIMARY KEY (`_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

        # ── af ──
        """CREATE TABLE IF NOT EXISTS `af` (
  `ffact` date DEFAULT NULL,
  `comprob` varchar(1) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `tipo` varchar(1) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `codfact` decimal(3,0) DEFAULT NULL,
  `fnfact` varchar(13) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `nombre` varchar(150) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `ftt` decimal(12,2) DEFAULT NULL,
  `codigo` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `direc` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `locali` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cuit` varchar(13) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `pos` varchar(2) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `tipo_pos` decimal(2,0) DEFAULT NULL,
  `tipo_doc` decimal(2,0) DEFAULT NULL,
  `fst` decimal(12,2) DEFAULT NULL,
  `descu` decimal(10,2) DEFAULT NULL,
  `descu2` decimal(10,2) DEFAULT NULL,
  `reca` decimal(10,2) DEFAULT NULL,
  `n_gra` decimal(12,2) DEFAULT NULL,
  `n_no_gra` decimal(12,2) DEFAULT NULL,
  `impu` decimal(10,2) DEFAULT NULL,
  `iva` decimal(10,2) DEFAULT NULL,
  `iva_105` decimal(10,2) DEFAULT NULL,
  `percep` decimal(10,2) DEFAULT NULL,
  `reib` decimal(10,2) DEFAULT NULL,
  `reib_901` decimal(10,2) DEFAULT NULL,
  `reib_921` decimal(10,2) DEFAULT NULL,
  `reib_917` decimal(10,2) DEFAULT NULL,
  `reib_914` decimal(10,2) DEFAULT NULL,
  `p_descu` decimal(5,2) DEFAULT NULL,
  `p_descu2` decimal(5,2) DEFAULT NULL,
  `p_descu3` decimal(5,2) DEFAULT NULL,
  `p_descu4` decimal(5,2) DEFAULT NULL,
  `p_reca` decimal(5,2) DEFAULT NULL,
  `p_impu` decimal(5,2) DEFAULT NULL,
  `p_percep` decimal(5,2) DEFAULT NULL,
  `p_reib` decimal(5,2) DEFAULT NULL,
  `p_reib_901` decimal(5,2) DEFAULT NULL,
  `p_reib_921` decimal(5,2) DEFAULT NULL,
  `p_reib_917` decimal(5,2) DEFAULT NULL,
  `p_reib_914` decimal(5,2) DEFAULT NULL,
  `cod_zona` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cod_vend` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `comi_v` decimal(9,2) DEFAULT NULL,
  `moneda` varchar(3) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `dolar` decimal(10,4) DEFAULT NULL,
  `remii` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `oc_prov` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `oc_2` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `provi` varchar(3) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `libera` decimal(6,2) DEFAULT NULL,
  `cod_fp` varchar(2) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `plazo` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `relote` varchar(6) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `transporte` varchar(13) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `impri_opcion` varchar(1) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `precrd` decimal(1,0) DEFAULT NULL,
  `pass` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cae` varchar(15) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `vtocae` date DEFAULT NULL,
  `fe` varchar(1) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `reib_924` decimal(12,2) DEFAULT NULL,
  `p_reib_924` decimal(6,2) DEFAULT NULL,
  `fecha` datetime(3) DEFAULT NULL,
  `_id` bigint NOT NULL AUTO_INCREMENT,
  `_uuid` char(36) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT (uuid()),
  `_creado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `_modificado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  `_sincronizado_en` datetime(3) DEFAULT NULL,
  `_sync_estado` tinyint(1) NOT NULL DEFAULT '0',
  `_sync_version` int NOT NULL DEFAULT '1',
  `_origen` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'MIGRADOR',
  `_eliminado` tinyint(1) NOT NULL DEFAULT '0',
  `_eliminado_en` datetime(3) DEFAULT NULL,
  `_hash` char(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `_api_id` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `update_at` datetime DEFAULT NULL,
  `syncro_at` datetime DEFAULT NULL,
  `deleted_at` datetime DEFAULT NULL,
  `_pdf_estado` tinyint(1) NOT NULL DEFAULT '0' COMMENT '0=pendiente 1=subido 2=error 3=no_existe',
  `_pdf_subido_en` datetime(3) DEFAULT NULL,
  `_pdf_url` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `_pdf_intentos` tinyint(1) NOT NULL DEFAULT '0',
  PRIMARY KEY (`_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

        # ── af_cobros ──
        """CREATE TABLE IF NOT EXISTS `af_cobros` (
  `fnfact` varchar(13) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `nro` varchar(8) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `codfact` decimal(3,0) DEFAULT NULL,
  `ptovta` varchar(4) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `fecha` date DEFAULT NULL,
  `tipo` varchar(15) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `importe` decimal(12,2) DEFAULT NULL,
  `importe_orig` decimal(12,2) DEFAULT NULL,
  `moneda` varchar(3) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `dolar` decimal(10,4) DEFAULT NULL,
  `datos` json DEFAULT NULL,
  `pass` varchar(4) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `codigo` varchar(4) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `_id` bigint NOT NULL AUTO_INCREMENT,
  `_uuid` char(36) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT (uuid()),
  `_creado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `_modificado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  `_sincronizado_en` datetime(3) DEFAULT NULL,
  `_sync_estado` tinyint(1) NOT NULL DEFAULT '0',
  `_sync_version` int NOT NULL DEFAULT '1',
  `_origen` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'MIGRADOR',
  `_eliminado` tinyint(1) NOT NULL DEFAULT '0',
  `_eliminado_en` datetime(3) DEFAULT NULL,
  `_hash` char(32) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `_api_id` varchar(100) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `update_at` datetime DEFAULT NULL,
  `syncro_at` datetime DEFAULT NULL,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`_id`),
  KEY `idx_af_cobros_fnfact` (`fnfact`),
  KEY `idx_af_cobros_codigo` (`codigo`),
  KEY `idx_af_cobros_fecha` (`fecha`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

        # ── af_obs ──
        """CREATE TABLE IF NOT EXISTS `af_obs` (
  `arch` varchar(3) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `fnfact` varchar(15) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `ffact` date DEFAULT NULL,
  `nro_obs` varchar(2) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `obs` varchar(150) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `_id` bigint NOT NULL AUTO_INCREMENT,
  `_uuid` char(36) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT (uuid()),
  `_creado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `_modificado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  `_sincronizado_en` datetime(3) DEFAULT NULL,
  `_sync_estado` tinyint(1) NOT NULL DEFAULT '0',
  `_sync_version` int NOT NULL DEFAULT '1',
  `_origen` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'MIGRADOR',
  `_eliminado` tinyint(1) NOT NULL DEFAULT '0',
  `_eliminado_en` datetime(3) DEFAULT NULL,
  `_hash` char(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `_api_id` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `update_at` datetime DEFAULT NULL,
  `syncro_at` datetime DEFAULT NULL,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

        # ── af_obs_local ──
        """CREATE TABLE IF NOT EXISTS `af_obs_local` (
  `arch` char(3) CHARACTER SET utf8mb3 COLLATE utf8mb3_unicode_ci DEFAULT NULL,
  `fnfact` varchar(19) CHARACTER SET utf8mb3 COLLATE utf8mb3_unicode_ci DEFAULT NULL,
  `ffact` date DEFAULT NULL,
  `nro_obs` char(2) CHARACTER SET utf8mb3 COLLATE utf8mb3_unicode_ci DEFAULT NULL,
  `obs` varchar(500) CHARACTER SET utf8mb3 COLLATE utf8mb3_unicode_ci DEFAULT NULL,
  `id` int NOT NULL AUTO_INCREMENT,
  `name` varchar(30) CHARACTER SET utf8mb3 COLLATE utf8mb3_unicode_ci DEFAULT NULL,
  `migrado` int DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3 COLLATE=utf8mb3_unicode_ci""",

        # ── afd ──
        """CREATE TABLE IF NOT EXISTS `afd` (
  `fcant` decimal(9,3) DEFAULT NULL,
  `fcod` varchar(25) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `fdesc` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `unidad` varchar(10) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `fprec` decimal(15,4) DEFAULT NULL,
  `bonif` decimal(4,2) DEFAULT NULL,
  `ftotal` decimal(12,2) DEFAULT NULL,
  `cod_orig` varchar(25) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `tipo` varchar(1) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `fnfact` varchar(13) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `comprob` varchar(1) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `codfact` decimal(3,0) DEFAULT NULL,
  `ftotal_siv` decimal(12,3) DEFAULT NULL,
  `f_pu` decimal(15,4) DEFAULT NULL,
  `f_lista` decimal(15,4) DEFAULT NULL,
  `tribu_iva` varchar(1) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `moneda` varchar(3) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cod_zona` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cod_vend` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `ffact` date DEFAULT NULL,
  `texva` varchar(15) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `texva2` varchar(15) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `codigo` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `pais` varchar(3) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `provi` varchar(3) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `tasa_ib` varchar(1) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `dolar` decimal(10,4) DEFAULT NULL,
  `relote` varchar(6) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `nro_despacho` varchar(16) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `r_codfact` decimal(3,0) DEFAULT NULL,
  `r_ptovta` decimal(5,0) DEFAULT NULL,
  `r_nrocpte` decimal(8,0) DEFAULT NULL,
  `_id` bigint NOT NULL AUTO_INCREMENT,
  `_uuid` char(36) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT (uuid()),
  `_creado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `_modificado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  `_sincronizado_en` datetime(3) DEFAULT NULL,
  `_sync_estado` tinyint(1) NOT NULL DEFAULT '0',
  `_sync_version` int NOT NULL DEFAULT '1',
  `_origen` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'MIGRADOR',
  `_eliminado` tinyint(1) NOT NULL DEFAULT '0',
  `_eliminado_en` datetime(3) DEFAULT NULL,
  `_hash` char(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `_api_id` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `update_at` datetime DEFAULT NULL,
  `syncro_at` datetime DEFAULT NULL,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

        # ── auditoria ──
        """CREATE TABLE IF NOT EXISTS `auditoria` (
  `id` int NOT NULL AUTO_INCREMENT,
  `fecha` datetime DEFAULT NULL,
  `comando` char(10) DEFAULT NULL,
  `data` text,
  `tabla` varchar(20) DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",

        # ── bancos ──
        """CREATE TABLE IF NOT EXISTS `bancos` (
  `cod_bco` varchar(2) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `fvto` date DEFAULT NULL,
  `nro` varchar(12) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `ing_egr` varchar(1) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `importe` decimal(12,2) DEFAULT NULL,
  `saldo` decimal(12,2) DEFAULT NULL,
  `fecha` date DEFAULT NULL,
  `comprob` varchar(2) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `detalle` varchar(40) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `fechai` date DEFAULT NULL,
  `form_tipo` varchar(3) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `form_nro` varchar(15) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `pass` varchar(4) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `obs` varchar(30) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `confirmado` tinyint(1) DEFAULT NULL,
  `e_cheque` tinyint(1) DEFAULT NULL,
  `codfact` decimal(3,0) DEFAULT NULL,
  `ptovta` decimal(5,0) DEFAULT NULL,
  `nro` varchar(12) DEFAULT NULL,
  `codigo` varchar(4) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `_id` bigint NOT NULL AUTO_INCREMENT,
  `_uuid` char(36) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT (uuid()),
  `_creado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `_modificado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  `_sincronizado_en` datetime(3) DEFAULT NULL,
  `_sync_estado` tinyint(1) NOT NULL DEFAULT '0',
  `_sync_version` int NOT NULL DEFAULT '1',
  `_origen` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'MIGRADOR',
  `_eliminado` tinyint(1) NOT NULL DEFAULT '0',
  `_eliminado_en` datetime(3) DEFAULT NULL,
  `_hash` char(32) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `_api_id` varchar(100) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `update_at` datetime DEFAULT NULL,
  `syncro_at` datetime DEFAULT NULL,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

        # ── cheques ──
        """CREATE TABLE IF NOT EXISTS `cheques` (
  `f_gene` date DEFAULT NULL,
  `fecha` date DEFAULT NULL,
  `importe` decimal(12,2) DEFAULT NULL,
  `estado` varchar(1) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `codigo` varchar(4) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `numero` varchar(12) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `orden` decimal(8,0) DEFAULT NULL,
  `receptor` varchar(30) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `fechar` date DEFAULT NULL,
  `banco` varchar(15) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `titular` varchar(30) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cuit` varchar(13) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `clearing` decimal(2,0) DEFAULT NULL,
  `alaorden` varchar(1) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `pass` varchar(4) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `valor` varchar(1) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `f_tipo_i` varchar(3) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `f_nro_i` varchar(13) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `f_tipo_e` varchar(3) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `f_nro_e` varchar(13) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `relote` varchar(6) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `nro_cta` varchar(11) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cpostal` varchar(4) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cod_bcoorig` varchar(3) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `sucursal` varchar(3) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cmc7` varchar(31) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `e_cheque` tinyint(1) DEFAULT NULL,
  `codfact` decimal(3,0) DEFAULT NULL,
  `ptovta` decimal(5,0) DEFAULT NULL,
  `nro` decimal(8,0) DEFAULT NULL,
  `codigo` varchar(4) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `_id` bigint NOT NULL AUTO_INCREMENT,
  `_uuid` char(36) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT (uuid()),
  `_creado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `_modificado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  `_sincronizado_en` datetime(3) DEFAULT NULL,
  `_sync_estado` tinyint(1) NOT NULL DEFAULT '0',
  `_sync_version` int NOT NULL DEFAULT '1',
  `_origen` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'MIGRADOR',
  `_eliminado` tinyint(1) NOT NULL DEFAULT '0',
  `_eliminado_en` datetime(3) DEFAULT NULL,
  `_hash` char(32) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `_api_id` varchar(100) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `update_at` datetime DEFAULT NULL,
  `syncro_at` datetime DEFAULT NULL,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

        # ── comision ──
        """CREATE TABLE IF NOT EXISTS `comision` (
  `fcc` date DEFAULT NULL,
  `debe` decimal(10,2) DEFAULT NULL,
  `haber` decimal(10,2) DEFAULT NULL,
  `saldo` decimal(10,2) DEFAULT NULL,
  `saldo_reg` decimal(10,2) DEFAULT NULL,
  `detalle` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `dolar` decimal(10,4) DEFAULT NULL,
  `codigo` varchar(4) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cod_vend` varchar(4) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `recibo` varchar(7) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `imporeci` decimal(10,2) DEFAULT NULL,
  `ftt` decimal(10,2) DEFAULT NULL,
  `pass` varchar(4) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `_id` bigint NOT NULL AUTO_INCREMENT,
  `_uuid` char(36) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT (uuid()),
  `_creado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `_modificado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  `_sincronizado_en` datetime(3) DEFAULT NULL,
  `_sync_estado` tinyint(1) NOT NULL DEFAULT '0',
  `_sync_version` int NOT NULL DEFAULT '1',
  `_origen` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'MIGRADOR',
  `_eliminado` tinyint(1) NOT NULL DEFAULT '0',
  `_eliminado_en` datetime(3) DEFAULT NULL,
  `_hash` char(32) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `_api_id` varchar(100) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `update_at` datetime DEFAULT NULL,
  `syncro_at` datetime DEFAULT NULL,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

        # ── contable ──
        """CREATE TABLE IF NOT EXISTS `contable` (
  `fecha` date DEFAULT NULL,
  `c_debe` varchar(8) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `c_haber` varchar(8) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `importe` decimal(15,2) DEFAULT NULL,
  `detalle` varchar(60) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `moneda` varchar(3) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `dolar` decimal(10,4) DEFAULT NULL,
  `form_tipo` varchar(3) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `form_nro` varchar(14) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `transfe` varchar(1) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `asi` varchar(1) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `pass` varchar(4) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `sucursal` varchar(4) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `_id` bigint NOT NULL AUTO_INCREMENT,
  `_uuid` char(36) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT (uuid()),
  `_creado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `_modificado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  `_sincronizado_en` datetime(3) DEFAULT NULL,
  `_sync_estado` tinyint(1) NOT NULL DEFAULT '0',
  `_sync_version` int NOT NULL DEFAULT '1',
  `_origen` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'MIGRADOR',
  `_eliminado` tinyint(1) NOT NULL DEFAULT '0',
  `_eliminado_en` datetime(3) DEFAULT NULL,
  `_hash` char(32) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `_api_id` varchar(100) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `update_at` datetime DEFAULT NULL,
  `syncro_at` datetime DEFAULT NULL,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

        # ── ctacte_d ──
        """CREATE TABLE IF NOT EXISTS `ctacte_d` (
  `fcc` date DEFAULT NULL,
  `fo` date DEFAULT NULL,
  `codigo` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `detalle` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `moneda` varchar(3) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `debe` decimal(12,2) DEFAULT NULL,
  `haber` decimal(12,2) DEFAULT NULL,
  `saldo` decimal(12,2) DEFAULT NULL,
  `dolar` decimal(15,4) DEFAULT NULL,
  `saldo_reg` decimal(12,2) DEFAULT NULL,
  `saldori` decimal(12,2) DEFAULT NULL,
  `comi_reg` decimal(10,2) DEFAULT NULL,
  `cod_vend` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `pass` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `ptoventa` decimal(5,0) DEFAULT NULL,
  `numero` decimal(8,0) DEFAULT NULL,
  `codfact` decimal(3,0) DEFAULT NULL,
  `fecha` datetime(3) DEFAULT NULL,
  `fchange` datetime(3) DEFAULT NULL,
  `identify` int DEFAULT NULL,
  `cod_vend2` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `comi_reg2` decimal(12,2) DEFAULT NULL,
  `_id` bigint NOT NULL AUTO_INCREMENT,
  `_uuid` char(36) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT (uuid()),
  `_creado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `_modificado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  `_sincronizado_en` datetime(3) DEFAULT NULL,
  `_sync_estado` tinyint(1) NOT NULL DEFAULT '0',
  `_sync_version` int NOT NULL DEFAULT '1',
  `_origen` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'MIGRADOR',
  `_eliminado` tinyint(1) NOT NULL DEFAULT '0',
  `_eliminado_en` datetime(3) DEFAULT NULL,
  `_hash` char(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `_api_id` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `update_at` datetime DEFAULT NULL,
  `syncro_at` datetime DEFAULT NULL,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

        # ── ctacte_h ──
        """CREATE TABLE IF NOT EXISTS `ctacte_h` (
  `fcc` date DEFAULT NULL,
  `fo` date DEFAULT NULL,
  `codigo` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `detalle` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `moneda` varchar(3) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `debe` decimal(15,2) DEFAULT NULL,
  `haber` decimal(15,2) DEFAULT NULL,
  `saldo` decimal(15,2) DEFAULT NULL,
  `dolar` decimal(15,4) DEFAULT NULL,
  `saldo_reg` decimal(15,2) DEFAULT NULL,
  `saldori` decimal(12,2) DEFAULT NULL,
  `pass` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `codfact` decimal(3,0) DEFAULT NULL,
  `ptoventa` decimal(5,0) DEFAULT NULL,
  `numero` decimal(8,0) DEFAULT NULL,
  `fecha` datetime(3) DEFAULT NULL,
  `fchange` datetime(3) DEFAULT NULL,
  `identify` int DEFAULT NULL,
  `saldo_ori` decimal(15,2) DEFAULT NULL,
  `_id` bigint NOT NULL AUTO_INCREMENT,
  `_uuid` char(36) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT (uuid()),
  `_creado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `_modificado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  `_sincronizado_en` datetime(3) DEFAULT NULL,
  `_sync_estado` tinyint(1) NOT NULL DEFAULT '0',
  `_sync_version` int NOT NULL DEFAULT '1',
  `_origen` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'MIGRADOR',
  `_eliminado` tinyint(1) NOT NULL DEFAULT '0',
  `_eliminado_en` datetime(3) DEFAULT NULL,
  `_hash` char(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `_api_id` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `update_at` datetime DEFAULT NULL,
  `syncro_at` datetime DEFAULT NULL,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

        # ── da_configuraciones ──
        """CREATE TABLE IF NOT EXISTS `da_configuraciones` (
  `clave` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `valor` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci,
  `descripcion` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `_id` bigint NOT NULL AUTO_INCREMENT,
  `_uuid` char(36) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `_creado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `_modificado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  `_sincronizado_en` datetime(3) DEFAULT NULL,
  `_sync_estado` tinyint(1) NOT NULL DEFAULT '0',
  `_sync_version` int NOT NULL DEFAULT '1',
  `_origen` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'MIGRADOR',
  `_eliminado` tinyint(1) NOT NULL DEFAULT '0',
  `_eliminado_en` datetime(3) DEFAULT NULL,
  `_hash` char(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `_api_id` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  PRIMARY KEY (`_id`),
  UNIQUE KEY `idx_clave_unica` (`clave`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

        # ── depo_st ──
        """CREATE TABLE IF NOT EXISTS `depo_st` (
  `cod` varchar(25) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cod_depo` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cant` decimal(14,2) DEFAULT NULL,
  `_id` bigint NOT NULL AUTO_INCREMENT,
  `_uuid` char(36) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT (uuid()),
  `_creado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `_modificado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  `_sincronizado_en` datetime(3) DEFAULT NULL,
  `_sync_estado` tinyint(1) NOT NULL DEFAULT '0',
  `_sync_version` int NOT NULL DEFAULT '1',
  `_origen` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'MIGRADOR',
  `_eliminado` tinyint(1) NOT NULL DEFAULT '0',
  `_eliminado_en` datetime(3) DEFAULT NULL,
  `_hash` char(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `_api_id` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `update_at` datetime DEFAULT NULL,
  `syncro_at` datetime DEFAULT NULL,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

        # ── exchange_rates ──
        """CREATE TABLE IF NOT EXISTS `exchange_rates` (
  `currency` char(3) NOT NULL,
  `rate_ars` decimal(18,6) NOT NULL,
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`currency`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",

        # ── marca ──
        """CREATE TABLE IF NOT EXISTS `marca` (
  `_id` bigint NOT NULL AUTO_INCREMENT,
  `marca` varchar(30) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `path_lista` varchar(250) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `lista_nro` varchar(100) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `lista_fecha` date DEFAULT NULL,
  `_uuid` char(36) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT (uuid()),
  `_creado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `_modificado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  `_sincronizado_en` datetime(3) DEFAULT NULL,
  `_sync_estado` tinyint(1) NOT NULL DEFAULT '0',
  `_sync_version` int NOT NULL DEFAULT '1',
  `_origen` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'MIGRADOR',
  `_eliminado` tinyint(1) NOT NULL DEFAULT '0',
  `_eliminado_en` datetime(3) DEFAULT NULL,
  `_hash` char(32) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `_api_id` varchar(100) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `update_at` datetime DEFAULT NULL,
  `syncro_at` datetime DEFAULT NULL,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

        # ── modelos ──
        """CREATE TABLE IF NOT EXISTS `modelos` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `codigo` char(10) NOT NULL,
  `nombre` varchar(100) NOT NULL,
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `deleted_at` timestamp NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `id` (`id`),
  KEY `nombre` (`nombre`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",

        # ── modelos_codigos ──
        """CREATE TABLE IF NOT EXISTS `modelos_codigos` (
  `id` int NOT NULL AUTO_INCREMENT,
  `id_stock` varchar(25) NOT NULL,
  `id_modelo` char(10) NOT NULL,
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `deleted_at` timestamp NULL DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",

        # ── monedas ──
        """CREATE TABLE IF NOT EXISTS `monedas` (
  `moneda` varchar(3) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `descripcion` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cotizacion` decimal(12,2) NOT NULL DEFAULT '1.00',
  `_id` bigint NOT NULL AUTO_INCREMENT,
  `_uuid` char(36) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `_creado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `_modificado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  `_sincronizado_en` datetime(3) DEFAULT NULL,
  `_sync_estado` tinyint(1) NOT NULL DEFAULT '0',
  `_sync_version` int NOT NULL DEFAULT '1',
  `_origen` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'MIGRADOR',
  `_eliminado` tinyint(1) NOT NULL DEFAULT '0',
  `_eliminado_en` datetime(3) DEFAULT NULL,
  `_hash` char(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `_api_id` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  PRIMARY KEY (`_id`),
  UNIQUE KEY `idx_moneda_unica` (`moneda`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

        # ── mov_caja ──
        """CREATE TABLE IF NOT EXISTS `mov_caja` (
  `nro_caja` varchar(3) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `fecha_caja` datetime(3) DEFAULT NULL,
  `fecha` date DEFAULT NULL,
  `detalle` varchar(45) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `moneda` varchar(3) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `ing_egr` varchar(1) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `importe` decimal(12,2) DEFAULT NULL,
  `saldo` decimal(12,2) DEFAULT NULL,
  `form_tipo` varchar(3) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `form_nro` varchar(15) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `dolar` decimal(10,4) DEFAULT NULL,
  `pass` varchar(4) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `relote` varchar(6) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `codfact` decimal(3,0) DEFAULT NULL,
  `ptovta` decimal(5,0) DEFAULT NULL,
  `nro` decimal(8,0) DEFAULT NULL,
  `codigo` varchar(4) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `_id` bigint NOT NULL AUTO_INCREMENT,
  `_uuid` char(36) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT (uuid()),
  `_creado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `_modificado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  `_sincronizado_en` datetime(3) DEFAULT NULL,
  `_sync_estado` tinyint(1) NOT NULL DEFAULT '0',
  `_sync_version` int NOT NULL DEFAULT '1',
  `_origen` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'MIGRADOR',
  `_eliminado` tinyint(1) NOT NULL DEFAULT '0',
  `_eliminado_en` datetime(3) DEFAULT NULL,
  `_hash` char(32) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `_api_id` varchar(100) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `update_at` datetime DEFAULT NULL,
  `syncro_at` datetime DEFAULT NULL,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

        # ── mstock ──
        """CREATE TABLE IF NOT EXISTS `mstock` (
  `fecha` date DEFAULT NULL,
  `cod` varchar(25) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `e_s` varchar(1) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `motivo` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `mov_cant` decimal(12,2) DEFAULT NULL,
  `detalle` varchar(100) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `pass` varchar(4) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cod_depo` varchar(4) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `codigo` varchar(4) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `obs` varchar(150) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `_id` bigint NOT NULL AUTO_INCREMENT,
  `_uuid` char(36) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT (uuid()),
  `_creado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `_modificado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  `_sincronizado_en` datetime(3) DEFAULT NULL,
  `_sync_estado` tinyint(1) NOT NULL DEFAULT '0',
  `_sync_version` int NOT NULL DEFAULT '1',
  `_origen` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'MIGRADOR',
  `_eliminado` tinyint(1) NOT NULL DEFAULT '0',
  `_eliminado_en` datetime(3) DEFAULT NULL,
  `_hash` char(32) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `_api_id` varchar(100) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `update_at` datetime DEFAULT NULL,
  `syncro_at` datetime DEFAULT NULL,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

        # ── ncompro ──
        """CREATE TABLE IF NOT EXISTS `ncompro` (
  `nro` varchar(8) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `f_a` tinyint(1) DEFAULT NULL,
  `nc_a` tinyint(1) DEFAULT NULL,
  `nd_a` tinyint(1) DEFAULT NULL,
  `f_e` tinyint(1) DEFAULT NULL,
  `f_b` tinyint(1) DEFAULT NULL,
  `nc_b` tinyint(1) DEFAULT NULL,
  `nd_b` tinyint(1) DEFAULT NULL,
  `t_b` tinyint(1) DEFAULT NULL,
  `r_x` tinyint(1) DEFAULT NULL,
  `f_c` tinyint(1) DEFAULT NULL,
  `r_f` tinyint(1) DEFAULT NULL,
  `r_fb` tinyint(1) DEFAULT NULL,
  `pr` tinyint(1) DEFAULT NULL,
  `oc` tinyint(1) DEFAULT NULL,
  `op` tinyint(1) DEFAULT NULL,
  `np` tinyint(1) DEFAULT NULL,
  `reci` tinyint(1) DEFAULT NULL,
  `pc` tinyint(1) DEFAULT NULL,
  `prof` tinyint(1) DEFAULT NULL,
  `confirma` tinyint(1) DEFAULT NULL,
  `retib` tinyint(1) DEFAULT NULL,
  `sucursal` varchar(4) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cotizpro` tinyint(1) DEFAULT NULL,
  `ing_f` tinyint(1) DEFAULT NULL,
  `egr_f` tinyint(1) DEFAULT NULL,
  `cantlinea` decimal(3,0) DEFAULT NULL,
  `reporte` varchar(8) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `reporte2` varchar(8) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `impresora` varchar(50) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `elij_imp` tinyint(1) DEFAULT NULL,
  `copias` decimal(2,0) DEFAULT NULL,
  `_id` bigint NOT NULL AUTO_INCREMENT,
  `_uuid` char(36) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT (uuid()),
  `_creado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `_modificado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  `_sincronizado_en` datetime(3) DEFAULT NULL,
  `_sync_estado` tinyint(1) NOT NULL DEFAULT '0',
  `_sync_version` int NOT NULL DEFAULT '1',
  `_origen` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'MIGRADOR',
  `_eliminado` tinyint(1) NOT NULL DEFAULT '0',
  `_eliminado_en` datetime(3) DEFAULT NULL,
  `_hash` char(32) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `_api_id` varchar(100) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `update_at` datetime DEFAULT NULL,
  `syncro_at` datetime DEFAULT NULL,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

        # ── numem ──
        """CREATE TABLE IF NOT EXISTS `numem` (
  `cod_bco` varchar(2) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `banco` varchar(25) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `lastchk` decimal(12,0) DEFAULT NULL,
  `lastchke` decimal(12,0) DEFAULT NULL,
  `codigo` varchar(8) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cuit` varchar(13) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `codtarjepro` varchar(4) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `tarjepro` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `tarjeimpu` varchar(8) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `nro_cuenta` varchar(22) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cbu` varchar(22) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `moneda` varchar(3) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `saldo_act` decimal(15,2) DEFAULT NULL,
  `activo` tinyint(1) DEFAULT '1',
  `_id` bigint NOT NULL AUTO_INCREMENT,
  `_uuid` char(36) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT (uuid()),
  `_creado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `_modificado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  `_sincronizado_en` datetime(3) DEFAULT NULL,
  `_sync_estado` tinyint(1) NOT NULL DEFAULT '0',
  `_sync_version` int NOT NULL DEFAULT '1',
  `_origen` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'MIGRADOR',
  `_eliminado` tinyint(1) NOT NULL DEFAULT '0',
  `_eliminado_en` datetime(3) DEFAULT NULL,
  `_hash` char(32) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `_api_id` varchar(100) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `update_at` datetime DEFAULT NULL,
  `syncro_at` datetime DEFAULT NULL,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

        # ── obras ──
        """CREATE TABLE IF NOT EXISTS `obras` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `nombre` varchar(50) NOT NULL,
  `contacto` varchar(50) NOT NULL,
  `autorizado` varchar(50) NOT NULL,
  `domicilio` varchar(50) NOT NULL,
  `telefono` varchar(50) NOT NULL,
  `codigo` char(4) NOT NULL,
  `codigofac` char(4) NOT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT NULL,
  `deleted_at` timestamp NULL DEFAULT NULL,
  UNIQUE KEY `id` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",

        # ── obras_facturas ──
        """CREATE TABLE IF NOT EXISTS `obras_facturas` (
  `fnfact` char(15) NOT NULL,
  `id_obra` int NOT NULL,
  `codfact` int NOT NULL,
  `pventa` int NOT NULL,
  `numero` int NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",

        # ── ped ──
        """CREATE TABLE IF NOT EXISTS `ped` (
  `item` decimal(4,0) DEFAULT NULL,
  `ffact` date DEFAULT NULL,
  `hora` datetime(3) DEFAULT NULL,
  `fvence` date DEFAULT NULL,
  `fnfact` decimal(6,0) DEFAULT NULL,
  `nombre` varchar(150) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `ftt` decimal(12,2) DEFAULT NULL,
  `pendiente` tinyint(1) DEFAULT NULL,
  `aprobado` varchar(2) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `fec_apro` datetime(3) DEFAULT NULL,
  `preparado` tinyint(1) DEFAULT NULL,
  `f_preparado` datetime(3) DEFAULT NULL,
  `cod_estado` varchar(2) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cod_preparador` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `p_despacho` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `codigo` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cuit` varchar(13) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `pos` varchar(2) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `fst` decimal(12,2) DEFAULT NULL,
  `descu` decimal(10,2) DEFAULT NULL,
  `descu2` decimal(10,2) DEFAULT NULL,
  `reca` decimal(10,2) DEFAULT NULL,
  `n_gra` decimal(12,2) DEFAULT NULL,
  `n_no_gra` decimal(12,2) DEFAULT NULL,
  `impu` decimal(10,2) DEFAULT NULL,
  `iva` decimal(10,2) DEFAULT NULL,
  `iva_105` decimal(10,2) DEFAULT NULL,
  `p_descu` decimal(5,2) DEFAULT NULL,
  `p_descu2` decimal(5,2) DEFAULT NULL,
  `p_descu3` decimal(5,2) DEFAULT NULL,
  `p_descu4` decimal(5,2) DEFAULT NULL,
  `p_reca` decimal(5,2) DEFAULT NULL,
  `p_impu` decimal(5,2) DEFAULT NULL,
  `cod_vend` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `moneda` varchar(3) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `dolar` decimal(10,4) DEFAULT NULL,
  `provi` varchar(3) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `libera` decimal(6,2) DEFAULT NULL,
  `cod_fp` varchar(2) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `plazo` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `fentre` date DEFAULT NULL,
  `hentre` varchar(5) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `pesot` decimal(8,3) DEFAULT NULL,
  `lote` decimal(5,0) DEFAULT NULL,
  `np_oc` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `np_oc_2` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `lista` varchar(1) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `precrd` decimal(1,0) DEFAULT NULL,
  `pass_apro` varchar(10) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `pass` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `p_reib` decimal(5,2) DEFAULT NULL,
  `p_reib_901` decimal(5,2) DEFAULT NULL,
  `p_reib_921` decimal(5,2) DEFAULT NULL,
  `p_reib_917` decimal(5,2) DEFAULT NULL,
  `p_reib_914` decimal(5,2) DEFAULT NULL,
  `reib` decimal(10,2) DEFAULT NULL,
  `reib_901` decimal(10,2) DEFAULT NULL,
  `reib_921` decimal(10,2) DEFAULT NULL,
  `reib_914` decimal(10,2) DEFAULT NULL,
  `reib_917` decimal(10,2) DEFAULT NULL,
  `fchange` datetime(3) DEFAULT NULL,
  `tipo_pos` decimal(2,0) DEFAULT NULL,
  `tipo_doc` decimal(2,0) DEFAULT NULL,
  `_id` bigint NOT NULL AUTO_INCREMENT,
  `_uuid` char(36) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT (uuid()),
  `_creado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `_modificado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  `_sincronizado_en` datetime(3) DEFAULT NULL,
  `_sync_estado` tinyint(1) NOT NULL DEFAULT '0',
  `_sync_version` int NOT NULL DEFAULT '1',
  `_origen` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'MIGRADOR',
  `_eliminado` tinyint(1) NOT NULL DEFAULT '0',
  `_eliminado_en` datetime(3) DEFAULT NULL,
  `_hash` char(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `_api_id` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `update_at` datetime DEFAULT NULL,
  `syncro_at` datetime DEFAULT NULL,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

        # ── ped_d ──
        """CREATE TABLE IF NOT EXISTS `ped_d` (
  `fcant` decimal(9,3) DEFAULT NULL,
  `menos` decimal(9,3) DEFAULT NULL,
  `pendi` varchar(1) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `dentrega` varchar(3) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `item` decimal(4,0) DEFAULT NULL,
  `cnt_preparada` decimal(6,0) DEFAULT NULL,
  `preparado` tinyint(1) DEFAULT NULL,
  `f_preparado` datetime(3) DEFAULT NULL,
  `cod_preparada` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `user_preparo` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cod_estado` varchar(2) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `fcod` varchar(25) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `fdesc` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `unidad` varchar(10) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `f_pu` decimal(15,2) DEFAULT NULL,
  `fprec` decimal(15,4) DEFAULT NULL,
  `bonif` decimal(4,2) DEFAULT NULL,
  `ftotal` decimal(12,2) DEFAULT NULL,
  `cod_orig` varchar(25) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `tribu_iva` varchar(1) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `codigo` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `ffact` date DEFAULT NULL,
  `cod_vend` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `texva` varchar(15) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `texva2` varchar(15) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `fnfact` decimal(6,0) DEFAULT NULL,
  `lote` decimal(5,0) DEFAULT NULL,
  `nro_despacho` varchar(16) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `fchange` datetime(3) DEFAULT NULL,
  `_id` bigint NOT NULL AUTO_INCREMENT,
  `_uuid` char(36) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT (uuid()),
  `_creado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `_modificado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  `_sincronizado_en` datetime(3) DEFAULT NULL,
  `_sync_estado` tinyint(1) NOT NULL DEFAULT '0',
  `_sync_version` int NOT NULL DEFAULT '1',
  `_origen` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'MIGRADOR',
  `_eliminado` tinyint(1) NOT NULL DEFAULT '0',
  `_eliminado_en` datetime(3) DEFAULT NULL,
  `_hash` char(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `_api_id` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `update_at` datetime DEFAULT NULL,
  `syncro_at` datetime DEFAULT NULL,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

        # ── ped_d_local ──
        """CREATE TABLE IF NOT EXISTS `ped_d_local` (
  `id` int NOT NULL AUTO_INCREMENT,
  `fcant` decimal(6,0) DEFAULT NULL,
  `menos` decimal(6,0) DEFAULT NULL,
  `pendi` char(1) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `dentrega` char(3) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `item` decimal(4,0) DEFAULT NULL,
  `cnt_preparada` decimal(6,0) DEFAULT NULL,
  `preparado` bit(1) DEFAULT NULL,
  `f_preparado` datetime DEFAULT NULL,
  `cod_preparada` char(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `user_preparo` char(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cod_estado` char(2) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `fcod` varchar(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `fdesc` varchar(65) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `unidad` char(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `f_pu` decimal(12,2) DEFAULT NULL,
  `fprec` decimal(12,2) DEFAULT NULL,
  `bonif` decimal(4,2) DEFAULT NULL,
  `ftotal` decimal(12,2) DEFAULT NULL,
  `cod_orig` varchar(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `tribu_iva` char(1) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `codigo` char(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `ffact` date DEFAULT NULL,
  `cod_vend` char(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `texva` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `texva2` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `fnfact` int DEFAULT NULL,
  `lote` decimal(5,0) DEFAULT NULL,
  `nro_despacho` varchar(21) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `migrado` int DEFAULT NULL,
  `update_at` datetime DEFAULT NULL,
  `syncro_at` datetime DEFAULT NULL,
  `deleted_at` datetime DEFAULT NULL,
  `id_web` int DEFAULT NULL,
  `id_ped_local` int DEFAULT NULL,
  `id_empresa` int DEFAULT NULL,
  `id_sucursal` int DEFAULT NULL,
  `_uuid` char(36) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT (uuid()),
  `_creado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `_origen` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'API',
  PRIMARY KEY (`id`),
  KEY `idx_fcod` (`fcod`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

        # ── ped_local ──
        """CREATE TABLE IF NOT EXISTS `ped_local` (
  `id` int NOT NULL AUTO_INCREMENT,
  `item` int DEFAULT NULL,
  `ffact` datetime DEFAULT NULL,
  `hora` datetime DEFAULT NULL,
  `fvence` datetime DEFAULT NULL,
  `fnfact` int NOT NULL DEFAULT '0',
  `nombre` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `ftt` decimal(12,2) NOT NULL DEFAULT '0.00',
  `pendiente` int NOT NULL DEFAULT '0',
  `aprobado` varchar(2) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `fec_apro` datetime DEFAULT NULL,
  `preparado` int NOT NULL DEFAULT '0',
  `f_preparado` datetime DEFAULT NULL,
  `cod_estado` varchar(2) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cod_preparado` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `p_despacho` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `codigo` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cuit` varchar(13) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `pos` varchar(2) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `fst` decimal(12,2) NOT NULL DEFAULT '0.00',
  `descu` decimal(10,2) NOT NULL DEFAULT '0.00',
  `descu2` decimal(10,2) NOT NULL DEFAULT '0.00',
  `reca` decimal(10,2) NOT NULL DEFAULT '0.00',
  `n_gra` decimal(12,2) NOT NULL DEFAULT '0.00',
  `n_no_gra` decimal(12,2) NOT NULL DEFAULT '0.00',
  `impu` decimal(10,2) NOT NULL DEFAULT '0.00',
  `iva` decimal(10,2) NOT NULL DEFAULT '0.00',
  `iva_105` decimal(10,2) NOT NULL DEFAULT '0.00',
  `p_descu` decimal(5,2) NOT NULL DEFAULT '0.00',
  `p_descu2` decimal(5,2) NOT NULL DEFAULT '0.00',
  `p_descu3` decimal(5,2) NOT NULL DEFAULT '0.00',
  `p_descu4` decimal(5,2) NOT NULL DEFAULT '0.00',
  `p_reca` decimal(5,2) NOT NULL DEFAULT '0.00',
  `p_impu` decimal(5,2) NOT NULL DEFAULT '0.00',
  `cod_vend` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `moneda` varchar(3) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `dolar` decimal(10,4) DEFAULT '0.0000',
  `provi` varchar(3) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `libera` decimal(6,2) DEFAULT '0.00',
  `cod_fp` varchar(2) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `plazo` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `fentre` datetime DEFAULT NULL,
  `hentre` varchar(5) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `pesot` decimal(8,3) DEFAULT '0.000',
  `lote` int DEFAULT NULL,
  `np_oc` varchar(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `np_oc_2` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `lista` varchar(1) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `precrd` int DEFAULT NULL,
  `pass_apro` varchar(10) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `pass` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `p_reib` decimal(5,2) DEFAULT '0.00',
  `p_reib_901` decimal(5,2) DEFAULT '0.00',
  `p_reib_921` decimal(5,2) DEFAULT '0.00',
  `p_reib_917` decimal(5,2) DEFAULT '0.00',
  `p_reib_914` decimal(5,2) DEFAULT '0.00',
  `reib` decimal(10,2) DEFAULT '0.00',
  `reib_901` decimal(10,2) DEFAULT '0.00',
  `reib_921` decimal(10,2) DEFAULT '0.00',
  `reib_917` decimal(10,2) DEFAULT '0.00',
  `reib_914` decimal(10,2) DEFAULT '0.00',
  `tipo_pos` int DEFAULT NULL,
  `tipo_doc` int DEFAULT NULL,
  `migrado` int DEFAULT NULL,
  `update_at` datetime DEFAULT NULL,
  `syncro_at` datetime DEFAULT NULL,
  `deleted_at` datetime DEFAULT NULL,
  `id_web` int DEFAULT NULL,
  `nro_pedido_erp` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `estado_local` tinyint(1) NOT NULL DEFAULT '0',
  `fecha_proceso` datetime DEFAULT NULL,
  `obs_local` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci,
  `id_empresa` int DEFAULT NULL,
  `id_sucursal` int DEFAULT NULL,
  `_uuid` char(36) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT (uuid()),
  `_creado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `_modificado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  `_sincronizado_en` datetime(3) DEFAULT NULL,
  `_sync_estado` tinyint(1) NOT NULL DEFAULT '1',
  `_origen` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'API',
  PRIMARY KEY (`id`),
  KEY `idx_estado_local` (`estado_local`),
  KEY `idx_sync_estado` (`_sync_estado`),
  KEY `idx_codigo_cli` (`codigo`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

        # ── ped_web_meta ──
        """CREATE TABLE IF NOT EXISTS `ped_web_meta` (
  `id` int NOT NULL AUTO_INCREMENT,
  `id_ped` int NOT NULL,
  `order_id` varchar(50) DEFAULT NULL,
  `comprador_nickname` varchar(100) DEFAULT NULL,
  `comprador_telefono` varchar(50) DEFAULT NULL,
  `envio_calle` varchar(255) DEFAULT NULL,
  `envio_cp` varchar(20) DEFAULT NULL,
  `envio_localidad` varchar(100) DEFAULT NULL,
  `envio_provincia` varchar(100) DEFAULT NULL,
  `carrier_nombre` varchar(100) DEFAULT NULL,
  `tracking_id` varchar(100) DEFAULT NULL,
  `meta_key` varchar(50) DEFAULT NULL,
  `meta_value` text DEFAULT NULL,
  `raw_payload` json DEFAULT NULL,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_ped` (`id_ped`),
  KEY `idx_order` (`order_id`),
  KEY `idx_key` (`meta_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

        # ── ped_web_pagos ──
        """CREATE TABLE IF NOT EXISTS `ped_web_pagos` (
  `id` int NOT NULL AUTO_INCREMENT,
  `id_ped` int NOT NULL,
  `ml_order_id` varchar(50) DEFAULT NULL,
  `payment_id` varchar(50) NOT NULL,
  `origen` varchar(20) DEFAULT 'ML',
  `forma_pago_cod` varchar(10) DEFAULT 'ML',
  `metodo_pago` varchar(50) DEFAULT NULL,
  `tarjeta_banco` varchar(50) DEFAULT NULL,
  `cuotas` int DEFAULT '1',
  `monto_bruto` decimal(12,2) NOT NULL,
  `comision_pasarela` decimal(10,2) DEFAULT '0.00',
  `impuestos_pasarela` decimal(10,2) DEFAULT '0.00',
  `monto_neto` decimal(12,2) NOT NULL,
  `estado_pago` varchar(20) DEFAULT 'approved',
  `fecha_cobro` datetime DEFAULT NULL,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_ped` (`id_ped`),
  KEY `idx_payment` (`payment_id`),
  KEY `idx_ml_order` (`ml_order_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

        # ── pre ──
        """CREATE TABLE IF NOT EXISTS `pre` (
  `ffact` date DEFAULT NULL,
  `hora` datetime(3) DEFAULT NULL,
  `fvence` date DEFAULT NULL,
  `aprobado` varchar(2) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `autorizado` tinyint(1) DEFAULT NULL,
  `user_autori` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `fnfact` decimal(6,0) DEFAULT NULL,
  `nombre` varchar(150) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `ftt` decimal(12,2) DEFAULT NULL,
  `codigo` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cuit` varchar(13) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `pos` varchar(2) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `fst` decimal(12,2) DEFAULT NULL,
  `descu` decimal(10,2) DEFAULT NULL,
  `descu2` decimal(10,2) DEFAULT NULL,
  `reca` decimal(10,2) DEFAULT NULL,
  `n_gra` decimal(12,2) DEFAULT NULL,
  `n_no_gra` decimal(12,2) DEFAULT NULL,
  `impu` decimal(10,2) DEFAULT NULL,
  `iva` decimal(10,2) DEFAULT NULL,
  `iva_105` decimal(10,2) DEFAULT NULL,
  `p_descu` decimal(5,2) DEFAULT NULL,
  `p_descu2` decimal(5,2) DEFAULT NULL,
  `p_descu3` decimal(5,2) DEFAULT NULL,
  `p_descu4` decimal(5,2) DEFAULT NULL,
  `p_reca` decimal(5,2) DEFAULT NULL,
  `p_impu` decimal(5,2) DEFAULT NULL,
  `cod_vend` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `moneda` varchar(3) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `dolar` decimal(10,4) DEFAULT NULL,
  `provi` varchar(3) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `libera` decimal(6,2) DEFAULT NULL,
  `cod_fp` varchar(2) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `plazo` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `lote` decimal(5,0) DEFAULT NULL,
  `lista` varchar(1) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `precrd` decimal(1,0) DEFAULT NULL,
  `pass` varchar(10) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `p_reib` decimal(5,2) DEFAULT NULL,
  `p_reib_921` decimal(5,2) DEFAULT NULL,
  `p_reib_901` decimal(5,2) DEFAULT NULL,
  `p_reib_914` decimal(10,2) DEFAULT NULL,
  `p_reib_917` decimal(5,2) DEFAULT NULL,
  `reib` decimal(10,2) DEFAULT NULL,
  `reib_901` decimal(10,2) DEFAULT NULL,
  `reib_921` decimal(10,2) DEFAULT NULL,
  `reib_917` decimal(10,2) DEFAULT NULL,
  `reib_914` decimal(10,2) DEFAULT NULL,
  `fchange` datetime(3) DEFAULT NULL,
  `tipo_pos` decimal(2,0) DEFAULT NULL,
  `tipo_doc` decimal(2,0) DEFAULT NULL,
  `_id` bigint NOT NULL AUTO_INCREMENT,
  `_uuid` char(36) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT (uuid()),
  `_creado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `_modificado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  `_sincronizado_en` datetime(3) DEFAULT NULL,
  `_sync_estado` tinyint(1) NOT NULL DEFAULT '0',
  `_sync_version` int NOT NULL DEFAULT '1',
  `_origen` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'MIGRADOR',
  `_eliminado` tinyint(1) NOT NULL DEFAULT '0',
  `_eliminado_en` datetime(3) DEFAULT NULL,
  `_hash` char(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `_api_id` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `update_at` datetime DEFAULT NULL,
  `syncro_at` datetime DEFAULT NULL,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

        # ── pre_d ──
        """CREATE TABLE IF NOT EXISTS `pre_d` (
  `fcant` decimal(9,3) DEFAULT NULL,
  `dentrega` varchar(3) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `item` decimal(4,0) DEFAULT NULL,
  `fcod` varchar(25) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `fdesc` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `unidad` varchar(10) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `f_pu` decimal(15,2) DEFAULT NULL,
  `fprec` decimal(15,4) DEFAULT NULL,
  `bonif` decimal(4,2) DEFAULT NULL,
  `ftotal` decimal(12,2) DEFAULT NULL,
  `cod_orig` varchar(25) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `fnfact` decimal(6,0) DEFAULT NULL,
  `tribu_iva` varchar(1) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `ffact` date DEFAULT NULL,
  `texva` varchar(15) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `texva2` varchar(15) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `lote` decimal(5,0) DEFAULT NULL,
  `nro_despacho` varchar(16) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `fchange` datetime(3) DEFAULT NULL,
  `_id` bigint NOT NULL AUTO_INCREMENT,
  `_uuid` char(36) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT (uuid()),
  `_creado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `_modificado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  `_sincronizado_en` datetime(3) DEFAULT NULL,
  `_sync_estado` tinyint(1) NOT NULL DEFAULT '0',
  `_sync_version` int NOT NULL DEFAULT '1',
  `_origen` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'MIGRADOR',
  `_eliminado` tinyint(1) NOT NULL DEFAULT '0',
  `_eliminado_en` datetime(3) DEFAULT NULL,
  `_hash` char(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `_api_id` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `update_at` datetime DEFAULT NULL,
  `syncro_at` datetime DEFAULT NULL,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

        # ── recibo_aplica ──
        """CREATE TABLE IF NOT EXISTS `recibo_aplica` (
  `id`               bigint        NOT NULL AUTO_INCREMENT,
  `id_recibo`        bigint        NOT NULL,
  `nro_recibo`       varchar(13)   NOT NULL,
  `codigo_cliente`   varchar(4)    NOT NULL,
  `fnfact`           varchar(30)   NOT NULL,
  `codfact`          decimal(3,0)  DEFAULT NULL,
  `fecha_fact`       date          DEFAULT NULL,
  `detalle`          varchar(80)   DEFAULT NULL,
  `moneda`           varchar(3)    DEFAULT '$',
  `debe`             decimal(15,2) DEFAULT '0.00',
  `haber`            decimal(15,2) DEFAULT '0.00',
  `saldo_anterior`   decimal(15,2) NOT NULL DEFAULT '0.00',
  `importe_aplicado` decimal(15,2) NOT NULL DEFAULT '0.00',
  `saldo_nuevo`      decimal(15,2) NOT NULL DEFAULT '0.00',
  `comi_reg`         decimal(12,2) DEFAULT '0.00',
  `dolar`            decimal(10,4) DEFAULT NULL,
  `cod_vend`         varchar(4)    DEFAULT NULL,
  `_uuid`            char(36)      NOT NULL DEFAULT (uuid()),
  `_creado_en`       datetime(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `_modificado_en`   datetime(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  `_origen`          varchar(20)   NOT NULL DEFAULT 'ERP',
  `update_at`        datetime      DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_recibo`  (`id_recibo`),
  KEY `idx_fnfact`  (`fnfact`),
  KEY `idx_cliente` (`codigo_cliente`),
  KEY `idx_nro`     (`nro_recibo`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

        # ── recibo_cab ──
        """CREATE TABLE IF NOT EXISTS `recibo_cab` (
  `id`              bigint          NOT NULL AUTO_INCREMENT,
  `nro_recibo`      varchar(13)     NOT NULL COMMENT 'Ej: 0011-00000123',
  `fecha`           date            NOT NULL,
  `codigo_cliente`  varchar(4)      NOT NULL,
  `nombre`          varchar(100)     NOT NULL,
  `cuit`            varchar(13)     DEFAULT NULL,
  `saldo_anterior`  decimal(15,2)   NOT NULL DEFAULT '0.00',
  `importe_total`   decimal(15,2)   NOT NULL DEFAULT '0.00',
  `moneda`          varchar(3)      NOT NULL DEFAULT '$',
  `dolar`           decimal(10,4)   DEFAULT NULL,
  `pass`            varchar(4)      DEFAULT NULL,
  `sucursal`        varchar(4)      DEFAULT NULL,
  `tipo`            varchar(5)      NOT NULL DEFAULT 'RECI',
  `cod_vend`        varchar(4)      DEFAULT NULL,
  `ncompro`         varchar(13)     DEFAULT NULL,
  `observaciones`   varchar(200)    DEFAULT NULL,
  `anulado`         tinyint(1)      NOT NULL DEFAULT '0',
  `fecha_anulacion` date            DEFAULT NULL,
  `pass_anulacion`  varchar(4)      DEFAULT NULL,
  `_uuid`           char(36)        NOT NULL DEFAULT (uuid()),
  `_creado_en`      datetime(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `_modificado_en`  datetime(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  `_origen`         varchar(20)     NOT NULL DEFAULT 'ERP',
  `_eliminado`      tinyint(1)      NOT NULL DEFAULT '0',
  `update_at`       datetime        DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_nro_recibo` (`nro_recibo`),
  KEY `idx_cliente` (`codigo_cliente`),
  KEY `idx_fecha` (`fecha`),
  KEY `idx_tipo` (`tipo`),
  KEY `idx_anulado` (`anulado`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

        # ── recibo_cobro ──
        """CREATE TABLE IF NOT EXISTS `recibo_cobro` (
  `id`          bigint        NOT NULL AUTO_INCREMENT,
  `id_recibo`   bigint DEFAULT NULL,
  `nro_recibo`  varchar(13)   NOT NULL,
  `tipo_cobro`  varchar(15)   NOT NULL,
  `importe`     decimal(15,2) NOT NULL DEFAULT '0.00',
  `moneda`      varchar(3)    NOT NULL DEFAULT '$',
  `dolar`       decimal(10,4) DEFAULT NULL,
  `tarje`       varchar(4)    DEFAULT NULL,
  `nro_tarje`   varchar(25)   DEFAULT NULL,
  `cupon`       varchar(15)   DEFAULT NULL,
  `autoriz`     varchar(15)   DEFAULT NULL,
  `cod_bco`     varchar(2)    DEFAULT NULL,
  `nro_doc`     varchar(20)   DEFAULT NULL,
  `titular`     varchar(40)   DEFAULT NULL,
  `cuit_doc`    varchar(13)   DEFAULT NULL,
  `fecha_vto`   date          DEFAULT NULL,
  `clearing`    varchar(1)    DEFAULT NULL,
  `cod_ret`     varchar(6)    DEFAULT NULL,
  `nro_ret`     varchar(15)   DEFAULT NULL,
  `datos`       json          DEFAULT NULL,
  `_uuid`       char(36)      NOT NULL DEFAULT (uuid()),
  `_creado_en`  datetime(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `_modificado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  `_origen`     varchar(20)   NOT NULL DEFAULT 'ERP',
  `update_at`   datetime      DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_recibo`     (`id_recibo`),
  KEY `idx_nro`        (`nro_recibo`),
  KEY `idx_tipo_cobro` (`tipo_cobro`),
  KEY `idx_cod_bco`    (`cod_bco`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

        # ── relacion_comprobantes ──
        """CREATE TABLE IF NOT EXISTS `relacion_comprobantes` (
  `id` int NOT NULL AUTO_INCREMENT,
  `codfact` int DEFAULT NULL,
  `ptovta` int DEFAULT NULL,
  `nro_cpte` bigint DEFAULT NULL,
  `tipo_doc` varchar(4) DEFAULT NULL,
  `ptovta_relacion` int DEFAULT NULL,
  `cpte_relacion` varchar(20) DEFAULT NULL,
  `fecha` datetime DEFAULT NULL,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",

        # ── rem ──
        """CREATE TABLE IF NOT EXISTS `rem` (
  `fnfact` varchar(13) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `ffact` date DEFAULT NULL,
  `codigo` varchar(4) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `nombre` varchar(150) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `direc` varchar(50) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `locali` varchar(30) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `provi` varchar(3) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `tipo` varchar(1) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `pass` varchar(4) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `comprob` varchar(1) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `moneda` varchar(3) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `dolar` decimal(10,4) DEFAULT NULL,
  `plazo` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `obs` varchar(150) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cuit` varchar(13) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `pos` varchar(2) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cod_vend` varchar(4) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cod_zona` varchar(4) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `np_oc` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `np_oc2` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `valor` decimal(12,2) DEFAULT NULL,
  `cod_trans` varchar(4) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `pendi` varchar(1) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `pesot` decimal(8,3) DEFAULT NULL,
  `bultos` decimal(8,0) DEFAULT NULL,
  `precrd` decimal(1,0) DEFAULT NULL,
  `_id` bigint NOT NULL AUTO_INCREMENT,
  `_uuid` char(36) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT (uuid()),
  `_creado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `_modificado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  `_sincronizado_en` datetime(3) DEFAULT NULL,
  `_sync_estado` tinyint(1) NOT NULL DEFAULT '0',
  `_sync_version` int NOT NULL DEFAULT '1',
  `_origen` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'MIGRADOR',
  `_eliminado` tinyint(1) NOT NULL DEFAULT '0',
  `_eliminado_en` datetime(3) DEFAULT NULL,
  `_hash` char(32) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `_api_id` varchar(100) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `update_at` datetime DEFAULT NULL,
  `syncro_at` datetime DEFAULT NULL,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

        # ── rem_d ──
        """CREATE TABLE IF NOT EXISTS `rem_d` (
  `fnfact` varchar(13) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `ffact` date DEFAULT NULL,
  `fcod` varchar(25) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `fdesc` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `unidad` varchar(10) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `fcant` decimal(9,3) DEFAULT NULL,
  `menos` decimal(6,0) DEFAULT NULL,
  `f_pu` decimal(15,4) DEFAULT NULL,
  `fprec` decimal(15,4) DEFAULT NULL,
  `bonif` decimal(5,2) DEFAULT NULL,
  `ftotal` decimal(15,4) DEFAULT NULL,
  `moneda` varchar(3) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `tipo` varchar(1) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `codigo` varchar(4) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cod_vend` varchar(4) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cod_zona` varchar(4) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `tribu_iva` varchar(1) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cod_orig` varchar(25) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `texva` varchar(15) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `texva2` varchar(15) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `pendi` varchar(1) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `pais` varchar(3) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `nro_despacho` varchar(16) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cbte_pv` decimal(5,0) DEFAULT NULL,
  `cbte_cod` decimal(3,0) DEFAULT NULL,
  `cbte_nro` decimal(8,0) DEFAULT NULL,
  `_id` bigint NOT NULL AUTO_INCREMENT,
  `_uuid` char(36) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT (uuid()),
  `_creado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `_modificado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  `_sincronizado_en` datetime(3) DEFAULT NULL,
  `_sync_estado` tinyint(1) NOT NULL DEFAULT '0',
  `_sync_version` int NOT NULL DEFAULT '1',
  `_origen` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'MIGRADOR',
  `_eliminado` tinyint(1) NOT NULL DEFAULT '0',
  `_eliminado_en` datetime(3) DEFAULT NULL,
  `_hash` char(32) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `_api_id` varchar(100) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `update_at` datetime DEFAULT NULL,
  `syncro_at` datetime DEFAULT NULL,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

        # ── stock ──
        """CREATE TABLE IF NOT EXISTS `stock` (
  `cod` varchar(25) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `mat` varchar(70) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `marca` varchar(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `linea` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `unidad` varchar(10) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cantxbulto` decimal(6,0) DEFAULT NULL,
  `com_minima` decimal(6,0) DEFAULT NULL,
  `moneda` varchar(3) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `plp0` decimal(12,2) DEFAULT NULL,
  `plp` decimal(15,4) DEFAULT NULL,
  `pu` decimal(15,4) DEFAULT NULL,
  `lista1` decimal(15,4) DEFAULT NULL,
  `lista2` decimal(15,4) DEFAULT NULL,
  `lista3` decimal(15,4) DEFAULT NULL,
  `lista4` decimal(15,4) DEFAULT NULL,
  `lista5` decimal(15,4) DEFAULT NULL,
  `lista6` decimal(15,4) DEFAULT NULL,
  `lista7` decimal(15,4) DEFAULT NULL,
  `lista8` decimal(15,4) DEFAULT NULL,
  `s_prec` date DEFAULT NULL,
  `sm` decimal(6,0) DEFAULT NULL,
  `rubro` varchar(40) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `subrubro` varchar(40) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cod_orig` varchar(40) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `pais` varchar(3) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `visible` tinyint(1) DEFAULT NULL,
  `bloqueado` tinyint(1) DEFAULT NULL,
  `imprime` tinyint(1) DEFAULT NULL,
  `cod_orig2` varchar(40) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cod_orig3` varchar(40) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `impu_c` varchar(8) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `impu_v` varchar(8) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `veriexis` tinyint(1) DEFAULT NULL,
  `comision` decimal(5,2) DEFAULT NULL,
  `falta` date DEFAULT NULL,
  `pass` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `pesoxuni` decimal(8,3) DEFAULT NULL,
  `tribu_iva` varchar(1) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `tasa_ib` varchar(1) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `st_obs` varchar(150) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `imp_eti` tinyint(1) DEFAULT NULL,
  `f_venta` date DEFAULT NULL,
  `f_compra` date DEFAULT NULL,
  `f_prec` date DEFAULT NULL,
  `f_precbase` date DEFAULT NULL,
  `st_prov` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `st_prov2` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `st_prov3` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `bonif` decimal(5,2) DEFAULT NULL,
  `desc_a` decimal(5,2) DEFAULT NULL,
  `desc_b` decimal(5,2) DEFAULT NULL,
  `desc_c` decimal(5,2) DEFAULT NULL,
  `por_prov` decimal(6,2) DEFAULT NULL,
  `por_prov2` decimal(6,2) DEFAULT NULL,
  `por_prov3` decimal(6,2) DEFAULT NULL,
  `por_prov4` decimal(6,2) DEFAULT NULL,
  `por_lista1` decimal(8,3) DEFAULT NULL,
  `por_lista2` decimal(8,3) DEFAULT NULL,
  `por_lista3` decimal(8,3) DEFAULT NULL,
  `por_lista4` decimal(8,3) DEFAULT NULL,
  `por_lista5` decimal(8,3) DEFAULT NULL,
  `por_lista6` decimal(8,3) DEFAULT NULL,
  `por_lista7` decimal(8,3) DEFAULT NULL,
  `por_lista8` decimal(8,3) DEFAULT NULL,
  `ok_grafico` tinyint(1) DEFAULT NULL,
  `grafico` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `tecnico` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `barra` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `dun14` varchar(14) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `st_memo` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci,
  `mini_lista` tinyint(1) DEFAULT NULL,
  `cavenmi` decimal(6,0) DEFAULT NULL,
  `cavenmip` decimal(3,2) DEFAULT NULL,
  `oferta` decimal(1,0) DEFAULT NULL,
  `scri` decimal(6,0) DEFAULT NULL,
  `std_montaj` decimal(8,3) DEFAULT NULL,
  `web` tinyint(1) DEFAULT NULL,
  `pesable` tinyint(1) DEFAULT NULL,
  `vigente` tinyint(1) DEFAULT NULL,
  `nro_orden` decimal(5,0) DEFAULT NULL,
  `nro_despacho` varchar(16) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `linea_01` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `linea_02` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `linea_03` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `linea_04` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `linea_05` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `linea_06` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `rack` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `esta` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `colu` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `depo` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `qi` decimal(20,6) DEFAULT NULL,
  `fchange` date DEFAULT NULL,
  `desc_fin` decimal(6,2) DEFAULT NULL,
  `_id` bigint NOT NULL AUTO_INCREMENT,
  `_uuid` char(36) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT (uuid()),
  `_creado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `_modificado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  `_sincronizado_en` datetime(3) DEFAULT NULL,
  `_sync_estado` tinyint(1) NOT NULL DEFAULT '0',
  `_sync_version` int NOT NULL DEFAULT '1',
  `_origen` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'MIGRADOR',
  `_eliminado` tinyint(1) NOT NULL DEFAULT '0',
  `_eliminado_en` datetime(3) DEFAULT NULL,
  `_hash` char(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `_api_id` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `update_at` datetime DEFAULT NULL,
  `syncro_at` datetime DEFAULT NULL,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

        # ── tarje ──
        """CREATE TABLE IF NOT EXISTS `tarje` (
  `fecha` date DEFAULT NULL,
  `nro_tarje` varchar(16) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cupon` varchar(10) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `moneda` varchar(3) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `importe` decimal(12,2) DEFAULT NULL,
  `estado` varchar(1) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `autoriz` varchar(8) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `factu` varchar(14) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `fechal` date DEFAULT NULL,
  `lote` varchar(5) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `fecha_liq` date DEFAULT NULL,
  `liq` varchar(13) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `tarje` varchar(4) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `form_tipo` varchar(3) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `form_nro` varchar(15) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `pass` varchar(4) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `relote` varchar(6) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `id_equi` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `codfact` decimal(3,0) DEFAULT NULL,
  `ptovta` decimal(5,0) DEFAULT NULL,
  `nro` decimal(8,0) DEFAULT NULL,
  `codigo` varchar(4) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `_id` bigint NOT NULL AUTO_INCREMENT,
  `_uuid` char(36) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT (uuid()),
  `_creado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `_modificado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  `_sincronizado_en` datetime(3) DEFAULT NULL,
  `_sync_estado` tinyint(1) NOT NULL DEFAULT '0',
  `_sync_version` int NOT NULL DEFAULT '1',
  `_origen` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'MIGRADOR',
  `_eliminado` tinyint(1) NOT NULL DEFAULT '0',
  `_eliminado_en` datetime(3) DEFAULT NULL,
  `_hash` char(32) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `_api_id` varchar(100) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `update_at` datetime DEFAULT NULL,
  `syncro_at` datetime DEFAULT NULL,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

        # ── tarjetas ──
        """CREATE TABLE IF NOT EXISTS `tarjetas` (
  `tarje` varchar(4) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `descrip` varchar(25) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `l_autoriz` decimal(8,2) DEFAULT NULL,
  `descu` decimal(5,2) DEFAULT NULL,
  `cuit` varchar(13) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `plazo_acred` decimal(3,0) DEFAULT NULL,
  `comision` decimal(5,2) DEFAULT NULL,
  `tipo` varchar(1) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `red` varchar(10) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `activo` tinyint(1) DEFAULT '1',
  `_id` bigint NOT NULL AUTO_INCREMENT,
  `_uuid` char(36) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT (uuid()),
  `_creado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `_modificado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  `_sincronizado_en` datetime(3) DEFAULT NULL,
  `_sync_estado` tinyint(1) NOT NULL DEFAULT '0',
  `_sync_version` int NOT NULL DEFAULT '1',
  `_origen` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'MIGRADOR',
  `_eliminado` tinyint(1) NOT NULL DEFAULT '0',
  `_eliminado_en` datetime(3) DEFAULT NULL,
  `_hash` char(32) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `_api_id` varchar(100) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `update_at` datetime DEFAULT NULL,
  `syncro_at` datetime DEFAULT NULL,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

        # ── vendedor ──
        """CREATE TABLE IF NOT EXISTS `vendedor` (
  `cod_vend` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `vendedor` varchar(25) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `comi_rela` decimal(6,2) DEFAULT NULL,
  `comi_abso` decimal(6,2) DEFAULT NULL,
  `comi_gan` decimal(6,2) DEFAULT NULL,
  `_id` bigint NOT NULL AUTO_INCREMENT,
  `_uuid` char(36) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT (uuid()),
  `_creado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `_modificado_en` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  `_sincronizado_en` datetime(3) DEFAULT NULL,
  `_sync_estado` tinyint(1) NOT NULL DEFAULT '0',
  `_sync_version` int NOT NULL DEFAULT '1',
  `_origen` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'MIGRADOR',
  `_eliminado` tinyint(1) NOT NULL DEFAULT '0',
  `_eliminado_en` datetime(3) DEFAULT NULL,
  `_hash` char(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `_api_id` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `update_at` datetime DEFAULT NULL,
  `syncro_at` datetime DEFAULT NULL,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

        # ── Tablas locales adicionales ──
    ]

    def inject_tables(self):
        """Crea las tablas auxiliares si no existen. No borra ni modifica datos."""
        cfg = self._get_config()
        if not cfg.get("database"):
            QMessageBox.warning(self, "Faltan datos",
                "Primero verificá la conexión y seleccioná una base de datos.")
            return

        resp = QMessageBox.question(
            self, "Inyectar Tablas",
            f"Se crearán las tablas auxiliares en '{cfg['database']}'\n"
            f"si todavía no existen (no se borran ni modifican datos).\n\n"
            f"Tablas: relacion_comprobantes, modelos, modelos_codigos,\n"
            f"obras, obras_facturas, auditoria, exchange_rates\n\n"
            f"¿Continuar?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if resp != QMessageBox.StandardButton.Yes:
            return

        self.btn_inject.setEnabled(False)
        self.btn_inject.setText("⏳ Inyectando...")
        QApplication.processEvents()

        ok = []
        errores = []

        try:
            import mysql.connector
            ultimo_error = None
            conn = None
            for auth in [None, "mysql_native_password", "caching_sha2_password"]:
                try:
                    kw = dict(
                        host=cfg["host"], port=int(cfg["port"]),
                        user=cfg["user"], password=cfg["password"],
                        database=cfg["database"],
                        connection_timeout=10, use_pure=True,
                    )
                    if auth:
                        kw["auth_plugin"] = auth
                    conn = mysql.connector.connect(**kw)
                    break
                except Exception as e:
                    ultimo_error = e
                    continue

            if conn is None:
                raise ultimo_error

            cur = conn.cursor()
            for ddl in self.TABLAS_AUXILIARES:
                # Extraer nombre de tabla del DDL
                nombre_tabla = ddl.split("`")[1] if "`" in ddl else "?"
                try:
                    cur.execute(ddl)
                    conn.commit()
                    ok.append(nombre_tabla)
                    logging.debug(f"[INJECT] Tabla '{nombre_tabla}' OK")
                except Exception as e:
                    errores.append(f"{nombre_tabla}: {e}")
                    logging.error(f"[INJECT] Error en '{nombre_tabla}': {e}")
            cur.close()
            conn.close()

        except Exception as e:
            QMessageBox.critical(self, "Error de conexión",
                f"No se pudo conectar:\n{e}")
            self.btn_inject.setEnabled(True)
            self.btn_inject.setText("🗂  Inyectar Tablas")
            return

        self.btn_inject.setEnabled(True)
        self.btn_inject.setText("🗂  Inyectar Tablas")

        if errores:
            msg = (f"✅ {len(ok)} tabla(s) OK: {', '.join(ok)}\n\n"
                   f"❌ {len(errores)} error(es):\n" + "\n".join(errores))
            QMessageBox.warning(self, "Inyección parcial", msg)
        else:
            QMessageBox.information(
                self, "✅ Tablas inyectadas",
                f"Se procesaron {len(ok)} tabla(s) correctamente:\n\n"
                + "\n".join(f"  ✓ {t}" for t in ok)
                + "\n\n(Las que ya existían no fueron modificadas)"
            )

    # ── Stored Procedures a inyectar ─────────────────────────────
    PROCEDURES = [
        {
            "nombre": "DevolverClientesPorCodigo",
            "sql": """
CREATE PROCEDURE `DevolverClientesPorCodigo`(
    IN _cod VARCHAR(50) CHARACTER SET utf8mb3 COLLATE utf8mb3_spanish_ci
)
BEGIN
    SELECT *
    FROM ctacte_D
    WHERE codigo = _cod;
END
""",
        },
    ]

    def inject_procedures(self):
        """Crea los stored procedures si no existen."""
        cfg = self._get_config()
        if not cfg.get("database"):
            QMessageBox.warning(self, "Faltan datos",
                "Primero verificá la conexión.")
            return

        nombres = [p["nombre"] for p in self.PROCEDURES]
        resp = QMessageBox.question(
            self, "Inyectar Procedures",
            f"Se crearán los siguientes stored procedures en '{cfg['database']}':\n\n"
            + "\n".join(f"  • {n}" for n in nombres)
            + "\n\n(Los que ya existen serán reemplazados)\n\n¿Continuar?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if resp != QMessageBox.StandardButton.Yes:
            return

        self.btn_procs.setEnabled(False)
        self.btn_procs.setText("⏳ Inyectando...")
        QApplication.processEvents()

        ok = []
        errores = []

        try:
            import mysql.connector
            conn = None
            ultimo_error = None
            for auth in [None, "mysql_native_password", "caching_sha2_password"]:
                try:
                    kw = dict(
                        host=cfg["host"], port=int(cfg["port"]),
                        user=cfg["user"], password=cfg["password"],
                        database=cfg["database"],
                        connection_timeout=10, use_pure=True,
                    )
                    if auth:
                        kw["auth_plugin"] = auth
                    conn = mysql.connector.connect(**kw)
                    break
                except Exception as e:
                    ultimo_error = e
                    continue

            if conn is None:
                raise ultimo_error

            cur = conn.cursor()
            for proc in self.PROCEDURES:
                nombre = proc["nombre"]
                try:
                    # DROP IF EXISTS primero
                    cur.execute(f"DROP PROCEDURE IF EXISTS `{nombre}`")
                    # Crear el procedure (sin DELIMITER — mysql connector no lo necesita)
                    cur.execute(proc["sql"].strip())
                    conn.commit()
                    ok.append(nombre)
                    logging.debug(f"[PROC] '{nombre}' OK")
                except Exception as e:
                    errores.append(f"{nombre}: {e}")
                    logging.error(f"[PROC] Error en '{nombre}': {e}")

            cur.close()
            conn.close()

        except Exception as e:
            QMessageBox.critical(self, "Error de conexión", f"No se pudo conectar:\n{e}")
            self.btn_procs.setEnabled(True)
            self.btn_procs.setText("⚙  Inyectar Procedures")
            return

        self.btn_procs.setEnabled(True)
        self.btn_procs.setText("⚙  Inyectar Procedures")

        if errores:
            QMessageBox.warning(self, "Inyección parcial",
                f"✅ OK: {', '.join(ok)}\n\n❌ Errores:\n" + "\n".join(errores))
        else:
            QMessageBox.information(self, "✅ Procedures inyectados",
                "Se crearon correctamente:\n\n"
                + "\n".join(f"  ✓ {n}" for n in ok))

    def _run_schema_init(self):
        cfg = self._get_config()
        if not cfg.get("database"):
            QMessageBox.warning(self, "Sin base", "Configurá la base de datos primero.")
            return
        resp = QMessageBox.question(
            self, "Inicializar base",
            f"Se crearán todas las tablas del sistema en '{cfg['database']}'.\n"
            "Las tablas existentes NO serán modificadas.\n\n¿Continuar?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if resp != QMessageBox.StandardButton.Yes:
            return

        self.btn_init_schema.setEnabled(False)
        self.btn_init_schema.setText("⏳  Inicializando...")
        self.init_log.clear()
        self.lbl_init_status.setText("")
        QApplication.processEvents()

        self._init_worker = SchemaInitWorker(cfg)
        self._init_worker.log.connect(self.init_log.append)
        self._init_worker.finished.connect(self._on_schema_init_done)
        self._init_worker.start()

    def _on_schema_init_done(self, ok: bool, msg: str):
        self.btn_init_schema.setEnabled(True)
        self.btn_init_schema.setText("🏗️  Inicializar / Verificar tablas")
        color = "#4ade80" if ok else "#f87171"
        self.lbl_init_status.setText(msg)
        self.lbl_init_status.setStyleSheet(f"font-size:12px; padding:4px; color:{color};")

    def _run_integrity_check(self):
        cfg = self._get_config()
        if not cfg.get("database"):
            QMessageBox.warning(self, "Sin base", "Configurá la base de datos primero.")
            return

        self.btn_integrity.setEnabled(False)
        self.btn_integrity.setText("⏳  Verificando...")
        self.mig_log.clear()
        self.lbl_mig_status.setText("")
        QApplication.processEvents()

        self._integrity_worker = IntegrityCheckWorker(cfg)
        self._integrity_worker.log.connect(self.mig_log.append)
        self._integrity_worker.finished.connect(self._on_integrity_done)
        self._integrity_worker.start()

    def _on_integrity_done(self, ok: bool, resumen: str, fixes: list):
        self.btn_integrity.setEnabled(True)
        self.btn_integrity.setText("🔎  Verificar integridad de tablas y campos")
        color = "#4ade80" if ok else "#fbbf24"
        self.lbl_mig_status.setText(resumen)
        self.lbl_mig_status.setStyleSheet(f"font-size:12px; padding:4px; color:{color};")

        if not fixes:
            return

        # Mostrar diálogo con los fixes disponibles
        dlg = QDialog(self)
        dlg.setWindowTitle("Problemas encontrados — ¿qué corregir?")
        dlg.setMinimumWidth(600)
        dlg.setMinimumHeight(400)
        layout = QVBoxLayout(dlg)

        lbl = QLabel(f"Se encontraron <b>{len(fixes)}</b> problema(s). "
                     "Seleccioná cuáles corregir:")
        lbl.setWordWrap(True)
        layout.addWidget(lbl)

        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        inner = QWidget(); il = QVBoxLayout(inner); il.setSpacing(4)

        checks = []
        for fix in fixes:
            tipo  = fix["tipo"]
            tabla = fix["tabla"]
            campo = fix.get("campo", "")
            sql   = fix["sql"]

            if tipo == "tabla":
                texto = f"🏗️  CREAR tabla `{tabla}`"
            elif tipo == "campo":
                texto = f"➕  AGREGAR campo `{tabla}`.`{campo}`"
            else:
                texto = f"⚡  MODIFICAR tipo `{tabla}`.`{campo}`"

            chk = QCheckBox(texto)
            chk.setChecked(True)
            chk.setToolTip(sql)
            chk.setProperty("sql", sql)
            il.addWidget(chk)
            checks.append(chk)

        il.addStretch()
        scroll.setWidget(inner)
        layout.addWidget(scroll, 1)

        row_sel = QHBoxLayout()
        btn_all  = QPushButton("Seleccionar todas")
        btn_none = QPushButton("Ninguna")
        btn_all.clicked.connect(lambda: [c.setChecked(True)  for c in checks])
        btn_none.clicked.connect(lambda: [c.setChecked(False) for c in checks])
        row_sel.addWidget(btn_all); row_sel.addWidget(btn_none); row_sel.addStretch()
        layout.addLayout(row_sel)

        row_btn = QHBoxLayout()
        btn_ok     = QPushButton("🔧  Aplicar seleccionadas")
        btn_cancel = QPushButton("Solo ver — no corregir")
        btn_ok.clicked.connect(dlg.accept)
        btn_cancel.clicked.connect(dlg.reject)
        row_btn.addStretch()
        row_btn.addWidget(btn_cancel)
        row_btn.addWidget(btn_ok)
        layout.addLayout(row_btn)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        sqls_seleccionados = [
            c.property("sql") for c in checks if c.isChecked()
        ]
        if not sqls_seleccionados:
            return

        # Correr worker con los fixes seleccionados
        cfg = self._get_config()
        self.mig_log.append(f"\n{'─'*50}")
        self.btn_integrity.setEnabled(False)
        QApplication.processEvents()

        self._integrity_fix_worker = IntegrityCheckWorker(
            cfg, apply_fixes=True, fix_sqls=sqls_seleccionados)
        self._integrity_fix_worker.log.connect(self.mig_log.append)
        self._integrity_fix_worker.finished.connect(
            lambda ok, msg, _: self._on_integrity_fix_done(ok, msg))
        self._integrity_fix_worker.start()

    def _on_integrity_fix_done(self, ok: bool, msg: str):
        self.btn_integrity.setEnabled(True)
        color = "#4ade80" if ok else "#f87171"
        self.lbl_mig_status.setStyleSheet(f"font-size:12px; padding:4px; color:{color};")
        self.lbl_mig_status.setText("✅  Correcciones aplicadas — volvé a verificar para confirmar.")

    def _check_schema_version(self):
        cfg = self._get_config()
        db  = cfg.get("database", "")
        if not db:
            QMessageBox.warning(self, "Sin base", "Configurá la base de datos primero.")
            return
        try:
            import mysql.connector
            conn = None
            ultimo_error = None
            for auth in [None, "mysql_native_password", "caching_sha2_password"]:
                try:
                    kw = dict(host=cfg["host"], port=int(cfg["port"]),
                              user=cfg["user"], password=cfg["password"],
                              connection_timeout=5, use_pure=True)
                    if auth: kw["auth_plugin"] = auth
                    conn = mysql.connector.connect(**kw)
                    break
                except Exception as e:
                    ultimo_error = e
                    continue
            if conn is None:
                raise ultimo_error

            cur = conn.cursor()
            # Verificar que la base existe
            cur.execute("SHOW DATABASES LIKE %s", (db,))
            if not cur.fetchone():
                self.lbl_schema_ver.setText(
                    f"Versión en base:  —\n"
                    f"Versión latest:   {SCHEMA_VERSION_LATEST}\n"
                    f"La base '{db}' no existe todavía — ejecutá la migración primero."
                )
                self.lbl_schema_ver.setStyleSheet(
                    "font-family:monospace; font-size:12px; padding:4px; color:#fbbf24;")
                cur.close(); conn.close()
                return

            cur.execute(f"USE `{db}`")
            try:
                cur.execute("SELECT MAX(version) FROM `_schema_version`")
                row = cur.fetchone()
                ver = row[0] if row and row[0] is not None else 0
            except Exception:
                ver = 0  # tabla _schema_version no existe aún

            cur.close(); conn.close()
            pendientes = len([m for m in SCHEMA_MIGRATIONS if m["version"] > ver])
            self.lbl_schema_ver.setText(
                f"Versión en base:  {ver}\n"
                f"Versión latest:   {SCHEMA_VERSION_LATEST}\n"
                f"Pendientes:       {pendientes} migración(es)"
            )
            color = "#4ade80" if pendientes == 0 else "#fbbf24"
            self.lbl_schema_ver.setStyleSheet(
                f"font-family:monospace; font-size:12px; padding:4px; color:{color};")
        except Exception as e:
            QMessageBox.critical(self, "Error de conexión",
                f"No se pudo conectar al servidor:\n\n{e}\n\n"
                "Verificá que los datos de conexión sean correctos en el tab Conexión.")


    def _run_migrations(self):
        cfg = self._get_config()
        if not cfg.get("database"):
            QMessageBox.warning(self, "Sin base", "Configurá la base de datos primero.")
            return
        resp = QMessageBox.question(
            self, "Aplicar migraciones",
            f"Se aplicarán las migraciones pendientes en '{cfg['database']}'.\n\n"
            "¿Continuar?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if resp != QMessageBox.StandardButton.Yes:
            return

        self.btn_run_mig.setEnabled(False)
        self.btn_run_mig.setText("⏳  Aplicando...")
        self.mig_log.clear()
        self.lbl_mig_status.setText("")
        QApplication.processEvents()

        self._mig_worker = SchemaUpdaterWorker(cfg)
        self._mig_worker.log.connect(self.mig_log.append)
        self._mig_worker.finished.connect(self._on_migrations_done)
        self._mig_worker.start()

    def _on_migrations_done(self, ok: bool, msg: str):
        self.btn_run_mig.setEnabled(True)
        self.btn_run_mig.setText("🚀  Aplicar migraciones pendientes")
        color = "#4ade80" if ok else "#f87171"
        self.lbl_mig_status.setText(msg)
        self.lbl_mig_status.setStyleSheet(f"font-size:12px; padding:4px; color:{color};")
        self._check_schema_version()  # actualizar el label de versión

    def test_connection(self):
        self.btn_test.setEnabled(False)
        self.btn_test.setText("🔄  Verificando…")
        self.lbl_conn.setText("🔄  Probando conexión…")
        self.lbl_conn.setStyleSheet(
            "QLabel { background-color:#1e3a5f; color:#93c5fd; "
            "border-radius:6px; padding:6px 12px; font-size:12px; font-weight:600; }")

        self.test_worker = ConnectionTestWorker(self._get_config())
        self.test_worker.result.connect(self._on_test_result)
        self.test_worker.start()

    def _on_test_result(self, ok: bool, msg: str):
        self.btn_test.setEnabled(True)
        self.btn_test.setText("🔌  Verificar Conexión")
        self._conn_ok = ok

        if ok:
            self.lbl_conn.setText(f"✅  Conexión exitosa con {self.cmb_db.currentText()}")
            self.lbl_conn.setStyleSheet(
                "QLabel { background-color:#064e3b; color:#6ee7b7; "
                "border-radius:6px; padding:6px 12px; font-size:12px; font-weight:600; }")
            self._save_current_config()  # ← guarda config al verificar OK
        else:
            self.lbl_conn.setText("❌  Sin conexión — revisá los datos")
            self.lbl_conn.setStyleSheet(
                "QLabel { background-color:#450a0a; color:#fca5a5; "
                "border-radius:6px; padding:6px 12px; font-size:12px; font-weight:600; }")

        box = QMessageBox()
        box.setIcon(QMessageBox.Icon.Information if ok else QMessageBox.Icon.Critical)
        box.setWindowTitle("Verificación de conexión")
        box.setText(msg)
        box.exec()

    # ── Migración ─────────────────────────────────────────────────
    def start_migration(self):
        if not self._conn_ok:
            resp = QMessageBox.question(
                self, "Conexión no verificada",
                "No verificaste la conexión todavía.\n"
                "¿Querés continuar igual?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if resp == QMessageBox.StandardButton.No:
                return

        files = self._files()
        if not files:
            QMessageBox.warning(self, "Sin archivos", "Agregá al menos un archivo DBF.")
            return

        self.btn_migrate.setEnabled(False)
        self.btn_test.setEnabled(False)
        self.progress_bar.setValue(0)
        self.res_list.clear(); self.log_view.clear()
        self.log_view.append("🚀 Iniciando migración …\n")

        self.worker = MigrationWorker(self._get_config(), files)
        self.worker.log.connect(self.log_view.append)
        self.worker.progress.connect(self.progress_bar.setValue)
        self.worker.table_done.connect(self._on_table_done)
        self.worker.finished.connect(self._on_finished)
        self.worker.start()

    def _on_table_done(self, table: str, rows: int):
        item = QListWidgetItem(f"✅  {table}  →  {rows:,} filas")
        item.setForeground(QColor("#4ade80"))
        self.res_list.addItem(item)

    def _on_finished(self, ok: bool, msg: str):
        self.btn_migrate.setEnabled(True)
        self.btn_test.setEnabled(True)
        self.log_view.append(f"\n{msg}")
        icon = QMessageBox.Icon.Information if ok else QMessageBox.Icon.Critical
        QMessageBox(icon, "Migración finalizada", msg, parent=self).exec()


# ══════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════

def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()