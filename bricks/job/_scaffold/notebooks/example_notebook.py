# Databricks notebook source
# ruff: noqa: F821 - dbutils is injected by the Databricks runtime
dbutils.widgets.text("catalog", "")  # type: ignore[name-defined]

catalog = dbutils.widgets.get("catalog")  # type: ignore[name-defined]
print(f"Job notebook running against catalog: {catalog}")
