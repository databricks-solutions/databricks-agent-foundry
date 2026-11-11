-- Daily cost attribution filtered to resources owned by this app instance.
-- Uses :time_range.min / :time_range.max DATE RANGE parameter (default: last 30 days).
-- Matches by usage_metadata identifiers (app_name, endpoint_name, warehouse_id)
-- and custom_tags['ai_solution_managed_by'] as a catch-all for tagged resources.
SELECT
    usage_date,
    billing_origin_product AS resource_type,
    sku_name,
    SUM(usage_quantity) AS total_dbu
FROM system.billing.usage
WHERE (usage_date BETWEEN :time_range.min AND :time_range.max OR :time_range IS NULL)
  AND (
    usage_metadata.app_name IN ('${var.app_resource_name}', '${var.app_name}')
    OR usage_metadata.endpoint_name = '${var.vs_endpoint_name}'
    OR usage_metadata.warehouse_id = '${var.warehouse_id}'
    OR custom_tags['ai_solution_managed_by'] = '${var.app_name}'
  )
GROUP BY 1, 2, 3
ORDER BY usage_date DESC, total_dbu DESC
