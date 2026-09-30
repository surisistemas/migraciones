#!/usr/bin/env python3
"""
generar_schema_sql.py

Regenera schema.sql a partir de schema.json.

Por que existe: schema.sql decia "Generado: 2026-04-30" y nunca se volvio a
generar. Con el tiempo quedo 22 tablas atras de schema.json (25 contra 47), y
nueve de las que faltaban eran tablas en las que el ERP escribe: af_cobros,
bancos, cheques, comision, recibo_aplica, recibo_cab, recibo_cobro, rem, rem_d.
Cualquier base creada desde ese schema.sql nacia sin ellas, y los INSERT del
ERP morian con 1146, igual que paso con perc_ret.

Nadie lee schema.sql desde codigo (el migrador usa schema.json); se usa a mano
para crear una base nueva. Por eso la unica defensa es regenerarlo, no
mantenerlo a mano.

Uso:
    python generar_schema_sql.py

Correrlo cada vez que se toca schema.json.
"""
import io
import json
import re
from datetime import date

SCHEMA_JSON = "schema.json"
SCHEMA_SQL = "schema.sql"
EOL = "\r\n"

# _schema_version no esta en schema.json (no viene de ningun DBF) pero una base
# nueva la necesita: es donde AplicarMigraciones lleva la cuenta.
SCHEMA_VERSION_SQL = """CREATE TABLE IF NOT EXISTS `_schema_version` (
  `version`      INT          NOT NULL,
  `descripcion`  VARCHAR(200) NOT NULL DEFAULT '',
  `aplicado_en`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`version`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;"""


COLLATION = "utf8mb4_unicode_ci"


def revisar_collation(schema):
    """Avisa si alguna tabla no esta en la collation del resto.

    En MySQL 8 dos columnas de texto con collations distintas no se pueden
    comparar: un JOIN entre ellas devuelve 1267 Illegal mix of collations.
    Siete tablas (auditoria, exchange_rates, modelos, modelos_codigos, obras,
    obras_facturas, relacion_comprobantes) habian quedado en
    utf8mb4_0900_ai_ci porque sus definiciones se pegaron copiadas de un
    SHOW CREATE TABLE de un servidor MySQL 8, cuyo default del servidor es esa
    collation. Se arreglaron en la v48. Este chequeo esta para que la proxima
    definicion que se pegue no lo reintroduzca en silencio.
    """
    malas = []
    for entry in schema:
        sql = entry["sql"]
        # Cualquier collation que no sea la del resto, no solo las utf8mb4:
        # af_obs_local estaba en utf8mb3_unicode_ci y la version anterior de
        # este chequeo la salteaba porque solo miraba tablas que ya dijeran
        # utf8mb4.
        for encontrada in re.findall(r"COLLATE[= ](\w+)", sql):
            if encontrada != COLLATION:
                malas.append(entry["tabla"])
    return sorted(set(malas))


def main():
    with io.open(SCHEMA_JSON, encoding="utf-8") as f:
        schema = json.load(f)

    malas = revisar_collation(schema)
    if malas:
        print("ATENCION: estas tablas no usan %s:" % COLLATION)
        for t in malas:
            print("   -", t)
        print("En MySQL 8 no se pueden unir con las demas (error 1267).")
        print("Arreglalas en schema.json y agrega la migracion que convierta")
        print("las bases que ya existen. schema.sql se genera igual.")
        print()

    partes = [
        "-- " + "=" * 60,
        "--  schema.sql - Schema completo del sistema de gestion",
        "--  Usar: CREATE TABLE IF NOT EXISTS (seguro en bases existentes)",
        "--",
        "--  GENERADO POR generar_schema_sql.py A PARTIR DE schema.json.",
        "--  No editarlo a mano: los cambios se pierden en la proxima corrida.",
        "--  Para agregar o cambiar una tabla, tocar schema.json y volver a",
        "--  correr el generador.",
        "--",
        "--  Generado: %s   (%d tablas + _schema_version)" % (date.today().isoformat(), len(schema)),
        "--  Subir a: github.com/surisistemas/migraciones/schema.sql",
        "-- " + "=" * 60,
        "",
        "SET NAMES utf8mb4;",
        "SET FOREIGN_KEY_CHECKS=0;",
        "",
        "-- Control de versiones",
        SCHEMA_VERSION_SQL,
        "",
    ]

    for entry in schema:
        tabla = entry["tabla"]
        sql = entry["sql"].strip()
        if not sql.endswith(";"):
            sql += ";"
        partes.append("-- " + tabla)
        partes.append(sql)
        partes.append("")

    texto = EOL.join(l.replace("\n", EOL) for l in partes)
    with io.open(SCHEMA_SQL, "w", encoding="utf-8", newline="") as f:
        f.write(texto)

    print("schema.sql regenerado: %d tablas + _schema_version" % len(schema))


if __name__ == "__main__":
    main()
