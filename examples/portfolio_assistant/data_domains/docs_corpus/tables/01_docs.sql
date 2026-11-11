-- Synthetic documentation corpus for Vector Search demo.
-- ~20 short articles covering major Databricks features.
-- Change Data Feed is required for Delta Sync Vector Search indexes.

CREATE OR REPLACE TABLE `${var.catalog}`.`${var.schema}`.docs (
  id INT,
  content STRING,
  source STRING,
  category STRING,
  doc_uri STRING
) USING DELTA
TBLPROPERTIES (delta.enableChangeDataFeed = true);

INSERT INTO `${var.catalog}`.`${var.schema}`.docs (id, content, source, category) VALUES
(1,
 'Unity Catalog is a unified governance solution for all data and AI assets on Databricks. It provides centralized access control, auditing, lineage, and data discovery across workspaces. With Unity Catalog, administrators can manage permissions using a familiar GRANT/REVOKE SQL syntax and track data lineage automatically.',
 'docs/unity-catalog/overview',
 'governance'),

(2,
 'Delta Lake is an open-source storage layer that brings reliability to data lakes. It provides ACID transactions, scalable metadata handling, and unifies streaming and batch data processing. Delta Lake runs on top of your existing data lake and is fully compatible with Apache Spark APIs.',
 'docs/delta-lake/overview',
 'storage'),

(3,
 'Databricks SQL (DBSQL) is a serverless data warehouse built on the Lakehouse. It supports standard SQL, auto-scaling serverless compute, and integrates with popular BI tools like Tableau and Power BI. Query performance benefits from Photon, a vectorized query engine written in C++.',
 'docs/dbsql/overview',
 'analytics'),

(4,
 'MLflow is an open-source platform for managing the end-to-end machine learning lifecycle. It provides experiment tracking, model registry, model serving, and evaluation. On Databricks, MLflow is fully managed and integrates with Unity Catalog for model governance.',
 'docs/mlflow/overview',
 'ml'),

(5,
 'Databricks Workflows (formerly Jobs) lets you orchestrate data pipelines, machine learning tasks, and notebooks. Jobs support task dependencies, retry policies, conditional execution, and can be triggered on schedules or events. Monitoring is available through the Jobs UI and REST API.',
 'docs/workflows/overview',
 'orchestration'),

(6,
 'Photon is an accelerated query engine for Databricks that provides extremely fast query performance for SQL and DataFrame workloads. It is written in C++ and operates on the Apache Spark API. Photon automatically accelerates portions of your queries without code changes.',
 'docs/photon/overview',
 'compute'),

(7,
 'Delta Sharing is the industry''s first open protocol for secure data sharing across organizations. It allows data providers to share data directly from their lakehouse without copying it. Recipients can access shared data using their preferred tools including pandas, Apache Spark, and Power BI.',
 'docs/delta-sharing/overview',
 'sharing'),

(8,
 'Serverless compute on Databricks provides instant, elastic compute resources without cluster management. It supports SQL warehouses, notebooks, and workflows. Serverless eliminates cold start times and automatically scales based on workload demand.',
 'docs/serverless/overview',
 'compute'),

(9,
 'Databricks Vector Search enables you to create a vector search index from a Delta table. The index automatically syncs when the underlying data changes. You can query the index using natural language or embedding vectors for semantic similarity search.',
 'docs/vector-search/overview',
 'ai'),

(10,
 'Mosaic AI Model Serving provides a scalable, low-latency service for deploying machine learning models and large language models. It supports custom models, foundation models, and external models through a unified API. Features include A/B testing, traffic splitting, and automatic scaling.',
 'docs/model-serving/overview',
 'ai'),

(11,
 'Lakeflow is the Databricks solution for data ingestion and transformation. Lakeflow Connect provides native connectors for SaaS applications, databases, and messaging systems. Lakeflow Declarative Pipelines (formerly DLT) enables building reliable batch and streaming data pipelines using a declarative approach.',
 'docs/lakeflow/overview',
 'ingestion'),

(12,
 'Databricks Apps lets you build and deploy full-stack data and AI applications directly on the Databricks platform. Apps run as managed services with built-in authentication, secrets management, and access to all Databricks resources including SQL warehouses, model serving endpoints, and Unity Catalog.',
 'docs/apps/overview',
 'apps'),

(13,
 'Structured Streaming on Databricks provides scalable, fault-tolerant stream processing built on Spark. It supports exactly-once semantics, event-time processing with watermarks, and can read from sources like Kafka, Auto Loader, and Delta tables. Output sinks include Delta Lake, Kafka, and custom sinks.',
 'docs/streaming/overview',
 'streaming'),

(14,
 'The Databricks Feature Store lets you create, share, and manage features for machine learning models. Features are stored as Delta tables in Unity Catalog, enabling governance, lineage, and discoverability. Online stores provide low-latency feature lookup for real-time serving.',
 'docs/feature-store/overview',
 'ml'),

(15,
 'Databricks Genie is a natural language interface for querying structured data. Users ask questions in plain English and Genie generates SQL queries against configured data sources. Genie Spaces define the data context, instructions, and example queries that guide the AI.',
 'docs/genie/overview',
 'ai'),

(16,
 'Liquid Clustering is an optimization feature for Delta tables that automatically organizes data for fast query performance. Unlike traditional partitioning, Liquid Clustering adapts to changing query patterns without requiring manual repartitioning. Enable it with CLUSTER BY on table creation.',
 'docs/liquid-clustering/overview',
 'storage'),

(17,
 'Auto Loader incrementally and efficiently processes new data files as they arrive in cloud storage. It supports JSON, CSV, Parquet, Avro, and other formats. Auto Loader uses file notification or directory listing mode to detect new files and provides schema inference and evolution.',
 'docs/auto-loader/overview',
 'ingestion'),

(18,
 'Databricks Repos provides Git integration for notebooks and files in the workspace. You can clone repositories, create branches, commit changes, and push/pull directly from the Databricks UI. It supports GitHub, GitLab, Azure DevOps, and Bitbucket.',
 'docs/repos/overview',
 'devtools'),

(19,
 'Model Context Protocol (MCP) is a universal integration standard for connecting AI agents to tools and data sources. Databricks supports managed MCP endpoints for Genie Spaces, Vector Search, Unity Catalog Functions, and external connections. The Supervisor Agent discovers tools via MCP at startup.',
 'docs/mcp/overview',
 'ai'),

(20,
 'Databricks AI/BI Dashboards (Lakeview) provide a native visualization layer built on the Lakehouse. Dashboards support SQL-based datasets, interactive filters, and can be published for stakeholders. They integrate with Genie for natural language question answering on dashboard data.',
 'docs/dashboards/overview',
 'analytics');

UPDATE `${var.catalog}`.`${var.schema}`.docs
SET doc_uri = concat(
  '/Volumes/${var.catalog}/${var.schema}/docs_sources/',
  source,
  '.md'
);
