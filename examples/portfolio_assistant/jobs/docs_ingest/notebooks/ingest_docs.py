# Databricks notebook source
# ruff: noqa: F821 - dbutils and spark are injected by the Databricks runtime
from databricks.sdk import WorkspaceClient
from delta.tables import DeltaTable
from pyspark.sql import functions as F

for widget in ("catalog", "schema", "volume", "table", "vs_index"):
    dbutils.widgets.text(widget, "")

catalog = dbutils.widgets.get("catalog")
schema = dbutils.widgets.get("schema")
volume = dbutils.widgets.get("volume")
table = dbutils.widgets.get("table")
vs_index = dbutils.widgets.get("vs_index")

volume_path = f"/Volumes/{catalog}/{schema}/{volume}"
table_name = f"{catalog}.{schema}.{table}"

documents = (
    spark.read.format("binaryFile")
    .option("recursiveFileLookup", "true")
    .load(volume_path)
    .select(
        (F.abs(F.xxhash64("path")) % F.lit(2_147_483_647)).cast("int").alias("id"),
        F.decode("content", "UTF-8").alias("content"),
        F.regexp_replace("path", f"^{volume_path}/", "").alias("source"),
        F.lit("uploaded").alias("category"),
        F.col("path").alias("doc_uri"),
    )
)

target = DeltaTable.forName(spark, table_name)
(
    target.alias("target")
    .merge(documents.alias("source"), "target.id = source.id")
    .whenMatchedUpdateAll()
    .whenNotMatchedInsertAll()
    .execute()
)

WorkspaceClient().vector_search_indexes.sync_index(vs_index)
print(f"Ingested {documents.count()} files and triggered sync for {vs_index}")
