# Databricks notebook source
# ruff: noqa: F821 - dbutils and spark are injected by the Databricks runtime
"""Rebuild the data-universe landscape tables.

Everything expensive happens here so that the two UC functions the agent
reaches for stay trivial SELECTs:

    landscape_views     one row per view that holds data  -> get_data_landscape()
    landscape_columns   one row per usable column         -> get_table_metadata()

Tier 1 is read on the request path and injected into the prompt, so it
cannot afford to count rows itself; that is the whole reason this job
exists. Both tables are small, so each run overwrites them.

View discovery is schema-wide via information_schema, so any view
deployed into the views schema is picked up without editing this notebook.
"""

from pyspark.sql import Row
from pyspark.sql.types import (
    LongType,
    StringType,
    StructField,
    StructType,
)

for widget in ("catalog", "views_schema", "agent_schema"):
    dbutils.widgets.text(widget, "")

catalog = dbutils.widgets.get("catalog").strip()
views_schema = dbutils.widgets.get("views_schema").strip()
agent_schema = dbutils.widgets.get("agent_schema").strip()
if not (catalog and views_schema and agent_schema):
    raise ValueError("catalog, views_schema and agent_schema are all required")

# A column at or above this NULL rate is dropped: it cannot be filtered
# or joined on, so listing it only invites the agent to try.
NULL_CUTOFF = 0.99
# Above this many distinct values, enumerating them stops being useful
# context and starts being prompt bloat. distinct_count still conveys the
# cardinality.
ENUM_MAX_VALUES = 25
# approx_count_distinct rejects complex types.
COMPLEX_TYPE_PREFIXES = ("ARRAY", "MAP", "STRUCT")
# Only these types are worth enumerating values for.
ENUMERABLE_TYPES = ("STRING", "BOOLEAN")

views_table = f"`{catalog}`.`{agent_schema}`.landscape_views"
columns_table = f"`{catalog}`.`{agent_schema}`.landscape_columns"

# COMMAND ----------

column_meta = spark.sql(
    f"""
    SELECT table_name, column_name, data_type, ordinal_position, comment
    FROM `{catalog}`.information_schema.columns
    WHERE table_schema = '{views_schema}'
    ORDER BY table_name, ordinal_position
    """
).collect()

view_comment = {
    row["table_name"]: row["comment"]
    for row in spark.sql(
        f"""
        SELECT table_name, comment
        FROM `{catalog}`.information_schema.tables
        WHERE table_schema = '{views_schema}'
        """
    ).collect()
}

columns_by_view: dict[str, list[tuple[str, str]]] = {}
for row in column_meta:
    columns_by_view.setdefault(row["table_name"], []).append((row["column_name"], row["data_type"]))

print(f"Discovered {len(columns_by_view)} views in {catalog}.{views_schema}")

# COMMAND ----------


def is_simple(data_type: str) -> bool:
    return not data_type.strip().upper().startswith(COMPLEX_TYPE_PREFIXES)


def is_enumerable(data_type: str) -> bool:
    return data_type.strip().upper().startswith(ENUMERABLE_TYPES)


def profile_view(view: str, columns: list[tuple[str, str]]) -> dict:
    """One aggregate scan per view: row count, null rates, cardinality, values.

    Single pass on purpose -- a scan per column would make this job's cost
    grow with the width of the catalog.
    """
    simple = [name for name, dtype in columns if is_simple(dtype)]
    enumerable = [name for name, dtype in columns if is_enumerable(dtype)]

    null_exprs = [f"avg(CASE WHEN `{c}` IS NULL THEN 1.0 ELSE 0.0 END) AS `nr__{c}`" for c in simple]
    card_exprs = [f"approx_count_distinct(`{c}`) AS `cd__{c}`" for c in simple]
    enum_exprs = [f"collect_set(`{c}`) AS `fv__{c}`" for c in enumerable]
    extra = "".join(", " + e for e in (*null_exprs, *card_exprs, *enum_exprs))

    row = spark.sql(f"SELECT count(*) AS row_count{extra} FROM `{catalog}`.`{views_schema}`.`{view}`").collect()[0]

    return {
        "row_count": int(row["row_count"]),
        "null_rates": {c: float(row[f"nr__{c}"]) for c in simple if row[f"nr__{c}"] is not None},
        "distinct_counts": {c: int(row[f"cd__{c}"]) for c in simple if row[f"cd__{c}"] is not None},
        "filter_values": {
            c: sorted(str(v) for v in row[f"fv__{c}"] if v is not None)
            for c in enumerable
            if row[f"fv__{c}"] and len(row[f"fv__{c}"]) <= ENUM_MAX_VALUES
        },
    }


profiles: dict[str, dict] = {}
for view, columns in columns_by_view.items():
    try:
        profiles[view] = profile_view(view, columns)
    except Exception as exc:  # noqa: BLE001 - one unreadable view must not fail the run
        print(f"WARNING: could not profile {view}: {exc}")

# COMMAND ----------

# Empty views are dropped here rather than in the UC function: if it is
# not in this table, the agent should not see it at all.
view_rows = [
    Row(
        view_name=view,
        catalog=catalog,
        view_schema=views_schema,
        row_count=profile["row_count"],
        view_description=view_comment.get(view) or "",
    )
    for view, profile in profiles.items()
    if profile["row_count"] > 0
]

column_rows = []
for row in column_meta:
    view = row["table_name"]
    profile = profiles.get(view)
    if not profile or profile["row_count"] == 0:
        continue
    if profile["null_rates"].get(row["column_name"], 0.0) >= NULL_CUTOFF:
        continue
    column_rows.append(
        Row(
            view_name=view,
            column_name=row["column_name"],
            ordinal_position=int(row["ordinal_position"]),
            data_type=row["data_type"],
            column_comment=row["comment"] or "",
            distinct_count=profile["distinct_counts"].get(row["column_name"]),
            filter_values=" | ".join(profile["filter_values"].get(row["column_name"], [])),
        )
    )

print(f"Built {len(view_rows)} view rows and {len(column_rows)} column rows")

# COMMAND ----------

# Explicit schemas: an empty catalog would otherwise leave Spark nothing
# to infer from, and the UC functions' RETURNS TABLE contract depends on
# these types.
views_struct = StructType(
    [
        StructField("view_name", StringType(), False),
        StructField("catalog", StringType(), False),
        StructField("view_schema", StringType(), False),
        StructField("row_count", LongType(), False),
        StructField("view_description", StringType(), True),
    ]
)
columns_struct = StructType(
    [
        StructField("view_name", StringType(), False),
        StructField("column_name", StringType(), False),
        StructField("ordinal_position", LongType(), False),
        StructField("data_type", StringType(), True),
        StructField("column_comment", StringType(), True),
        StructField("distinct_count", LongType(), True),
        StructField("filter_values", StringType(), True),
    ]
)

(
    spark.createDataFrame(view_rows, schema=views_struct)
    .write.mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(views_table)
)
(
    spark.createDataFrame(column_rows, schema=columns_struct)
    .write.mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(columns_table)
)

spark.sql(
    f"COMMENT ON TABLE {views_table} IS "
    "'Data-universe OVERVIEW: one row per curated view holding data. Rebuilt by "
    "the data-universe-profile job; read verbatim by get_data_landscape(), which "
    "is injected into the agent system prompt. Empty views are omitted.'"
)
spark.sql(
    f"COMMENT ON TABLE {columns_table} IS "
    "'Data-universe DETAIL: one row per usable column of a populated view. "
    "Entirely-NULL columns are omitted. distinct_count is an approximate "
    "cardinality; filter_values enumerates low-cardinality categoricals. Rebuilt "
    "by the data-universe-profile job; read verbatim by get_table_metadata().'"
)
print(f"Overwrote {views_table} ({len(view_rows)}) and {columns_table} ({len(column_rows)})")
