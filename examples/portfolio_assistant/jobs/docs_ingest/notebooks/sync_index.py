# Databricks notebook source
# ruff: noqa: F821 - dbutils is injected by the Databricks runtime
from databricks.sdk import WorkspaceClient

dbutils.widgets.text("vs_index", "")
vs_index = dbutils.widgets.get("vs_index")

WorkspaceClient().vector_search_indexes.sync_index(vs_index)
print(f"Triggered sync for {vs_index}")
