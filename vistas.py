"""
Vistas analíticas TBS - Día 2.

Crea 8 SQL VIEWs en SQLite que el API consume directamente.
Cada vista responde a una pregunta específica del dashboard.

IMPORTANTE: Actualizar CAMPO_TIPO_HORECA con el nombre
real del campo de clasificación una vez confirmado.
"""
from sqlalchemy import create_engine, text
from config import config

# ============================================================
# AJUSTAR ESTE VALOR cuando se confirme el campo de Odoo
# Ejemplo: 'x_studio_tipo_cliente' o 'x_tipo_horeca'
CAMPO_TIPO_HORECA = "x_studio_tipo_horeca"
# ============================================================

COMPANY_ID = 2

VISTAS = {}

# ============================================================
# VISTA 1 — North Star mensual
# Responde: ¿cuáles son los 5 KPIs del mes actual vs anterior?
# ============================================================
VISTAS["v_north_star"] = """
CREATE VIEW IF NOT EXISTS v_north_star AS
WITH mes_actual AS (
    SELECT
        SUM(so.amount_untaxed)        AS gmv_neto,
        COUNT(DISTINCT so.partner_id) AS cuentas_activas,
        COUNT(so.id)                  AS total_ordenes,
        strftime('%d', 'now')         AS dia_del_mes
    FROM raw_sale_order so
    WHERE so.state IN ('sale', 'done')
      AND so.company_id = 2
      AND so.partner_id != 3
      AND strftime('%Y-%m', so.date_order) =
          strftime('%Y-%m', 'now', 'localtime')
),
mes_anterior AS (
    SELECT
        SUM(so.amount_untaxed)        AS gmv_neto,
        COUNT(DISTINCT so.partner_id) AS cuentas_activas
    FROM raw_sale_order so
    WHERE so.state IN ('sale', 'done')
      AND so.company_id = 2
      AND so.partner_id != 3
      AND strftime('%Y-%m', so.date_order) =
          strftime('%Y-%m', 'now', 'localtime', '-1 month')
),
mismo_periodo_mes_anterior AS (
    -- Mes anterior hasta el mismo día que hoy
    -- para comparación justa (ej: 1-11 abril vs 1-11 mayo)
    SELECT
        SUM(so.amount_untaxed) AS gmv_neto
    FROM raw_sale_order so
    WHERE so.state IN ('sale', 'done')
      AND so.company_id = 2
      AND so.partner_id != 3
      AND strftime('%Y-%m', so.date_order) =
          strftime('%Y-%m', 'now', 'localtime', '-1 month')
      AND CAST(strftime('%d', so.date_order) AS INT) <=
          CAST(strftime('%d', 'now', 'localtime') AS INT)
),
proyeccion AS (
    -- Proyección lineal: si llevamos X en N días,
    -- el mes completo sería X / N * días_del_mes
    SELECT
        ROUND(
            ma.gmv_neto /
            NULLIF(CAST(strftime('%d','now','localtime') AS FLOAT), 0) *
            CAST(strftime('%d', date('now','localtime',
                 'start of month', '+1 month', '-1 day')) AS FLOAT)
        , 0) AS gmv_proyectado
    FROM mes_actual ma
),
cartera_hoy AS (
    SELECT
        COALESCE(SUM(am.amount_residual), 0)   AS total_cartera,
        COALESCE(SUM(CASE
            WHEN julianday('now') -
                 julianday(am.invoice_date_due) > 30
            THEN am.amount_residual ELSE 0
        END), 0)                               AS cartera_vencida_30
    FROM raw_account_move am
    WHERE am.move_type  = 'out_invoice'
      AND am.state      = 'posted'
      AND am.company_id = 2
      AND am.amount_residual > 0
),
margen AS (
    SELECT
        1.0 - (
            SUM(COALESCE(rp.standard_price, 0) * sol.product_uom_qty) /
            NULLIF(SUM(sol.price_subtotal), 0)
        ) AS margen_bruto_pct
    FROM raw_sale_order so
    JOIN raw_sale_order_line sol ON sol.order_id = so.id
    LEFT JOIN raw_product rp          ON sol.product_id = rp.id
    WHERE so.state IN ('sale', 'done')
      AND so.company_id = 2
      AND strftime('%Y-%m', so.date_order) =
          strftime('%Y-%m', 'now', 'localtime')
),
dso AS (
    SELECT ROUND(
        c.total_cartera /
        NULLIF((
            SELECT SUM(amount_untaxed)
            FROM raw_sale_order
            WHERE state IN ('sale','done')
              AND company_id = 2
              AND partner_id != 3
              AND date_order >= date('now','-90 days')
        ), 0) * 90
    , 0) AS dias_cobro
    FROM cartera_hoy c
)
SELECT
    -- Mes actual
    strftime('%Y-%m', 'now', 'localtime')              AS mes_actual,
    CAST(strftime('%d', 'now', 'localtime') AS INT)    AS dia_del_mes,
    ROUND(ma.gmv_neto, 0)                              AS gmv_neto_actual,
    -- Proyección y avance
    ROUND(py.gmv_proyectado, 0)                        AS gmv_proyectado_mes,
    ROUND(ma.gmv_neto / NULLIF(py.gmv_proyectado,0)
          * 100, 1)                                    AS avance_pct_proyeccion,
    -- Comparativos
    ROUND(mant.gmv_neto, 0)                            AS gmv_mes_anterior_completo,
    ROUND(mpma.gmv_neto, 0)                            AS gmv_mismo_periodo_ant,
    ROUND((ma.gmv_neto - mpma.gmv_neto) /
          NULLIF(mpma.gmv_neto, 0) * 100, 1)          AS variacion_vs_mismo_periodo,
    -- Cuentas
    ma.cuentas_activas,
    mant.cuentas_activas                               AS cuentas_mes_anterior,
    -- Margen
    ROUND(COALESCE(m.margen_bruto_pct, 0) * 100, 1)  AS margen_bruto_pct,
    -- Cartera
    ROUND(c.cartera_vencida_30, 0)                    AS cartera_vencida_30d,
    ROUND(c.total_cartera, 0)                         AS cartera_total,
    ROUND(c.cartera_vencida_30 /
          NULLIF(c.total_cartera,0) * 100, 1)         AS pct_cartera_vencida,
    COALESCE(d.dias_cobro, 0)                         AS dias_cobro_promedio
FROM mes_actual ma, mes_anterior mant,
     mismo_periodo_mes_anterior mpma,
     proyeccion py, cartera_hoy c, margen m, dso d
"""

# ============================================================
# VISTA 2 — Tendencia mensual 12 meses
# Responde: ¿cómo ha evolucionado el GMV mes a mes?
# ============================================================
VISTAS["v_tendencia_mensual"] = """
CREATE VIEW IF NOT EXISTS v_tendencia_mensual AS
SELECT
    strftime('%Y-%m', so.date_order)                            AS mes,
    strftime('%m', so.date_order)                               AS mes_num,
    strftime('%Y', so.date_order)                               AS anio,
    ROUND(SUM(so.amount_untaxed), 0)                            AS gmv_neto,
    ROUND(SUM(so.amount_total), 0)                              AS gmv_bruto,
    COUNT(DISTINCT so.partner_id)                               AS clientes_activos,
    COUNT(so.id)                                                AS total_ordenes,
    ROUND(AVG(so.amount_untaxed), 0)                            AS ticket_promedio,
    ROUND(SUM(so.amount_untaxed) /
          COUNT(DISTINCT so.partner_id), 0)                     AS gmv_por_cliente
FROM raw_sale_order so
WHERE so.state IN ('sale', 'done')
  AND so.company_id = 2
  AND so.partner_id != 3
  AND so.date_order >= date('now', '-12 months')
GROUP BY strftime('%Y-%m', so.date_order)
ORDER BY mes
"""

# ============================================================
# VISTA 3 — Densificación de clientes top
# Responde: estado de cada cuenta top con tendencia y alertas
# ============================================================
VISTAS["v_clientes_top"] = """
CREATE VIEW IF NOT EXISTS v_clientes_top AS
WITH gmv_por_cliente AS (
    SELECT
        so.partner_id,
        SUM(so.amount_untaxed)                                   AS gmv_anual,
        COUNT(so.id)                                             AS total_ordenes,
        COUNT(DISTINCT strftime('%Y-%m', so.date_order))         AS meses_activo,
        MAX(so.date_order)                                       AS ultimo_pedido,
        MIN(so.date_order)                                       AS primer_pedido,
        AVG(so.amount_untaxed)                                   AS ticket_promedio
    FROM raw_sale_order so
    WHERE so.state IN ('sale', 'done')
      AND so.company_id = 2
      AND so.partner_id != 3
      AND so.date_order >= date('now', '-12 months')
    GROUP BY so.partner_id
),
gmv_mes_actual AS (
    SELECT partner_id,
           SUM(amount_untaxed) AS gmv_mes
    FROM raw_sale_order
    WHERE state IN ('sale', 'done')
      AND company_id = 2
      AND strftime('%Y-%m', date_order) = strftime('%Y-%m', 'now', 'localtime')
    GROUP BY partner_id
),
gmv_mes_anterior AS (
    SELECT partner_id,
           SUM(amount_untaxed) AS gmv_mes
    FROM raw_sale_order
    WHERE state IN ('sale', 'done')
      AND company_id = 2
      AND strftime('%Y-%m', date_order) =
          strftime('%Y-%m', 'now', 'localtime', '-1 month')
    GROUP BY partner_id
),
margen_por_cliente AS (
    SELECT
        so.partner_id,
        1.0 - (SUM(COALESCE(rp.standard_price, 0) * sol.product_uom_qty) /
               NULLIF(SUM(sol.price_subtotal), 0)) AS margen_pct
    FROM raw_sale_order so
    JOIN raw_sale_order_line sol ON sol.order_id = so.id
    LEFT JOIN raw_product rp ON sol.product_id = rp.id
    WHERE so.state IN ('sale', 'done')
      AND so.company_id = 2
      AND so.date_order >= date('now', '-12 months')
    GROUP BY so.partner_id
),
cartera_por_cliente AS (
    SELECT
        partner_id,
        SUM(amount_residual)                                     AS cartera_total,
        SUM(CASE
            WHEN julianday('now') - julianday(invoice_date_due) > 30
            THEN amount_residual ELSE 0 END)                    AS cartera_vencida
    FROM raw_account_move
    WHERE move_type = 'out_invoice'
      AND state = 'posted'
      AND payment_state != 'paid'
      AND company_id = 2
    GROUP BY partner_id
)
SELECT
    g.partner_id                                                 AS id_cliente,
    rp.name                                                      AS nombre,
    ROUND(g.gmv_anual, 0)                                       AS gmv_anual,
    ROUND(g.gmv_anual / 12, 0)                                  AS gmv_mensual_prom,
    ROUND(COALESCE(ma.gmv_mes, 0), 0)                          AS gmv_mes_actual,
    ROUND(COALESCE(mant.gmv_mes, 0), 0)                        AS gmv_mes_anterior,
    ROUND((COALESCE(ma.gmv_mes, 0) - COALESCE(mant.gmv_mes, 0)) /
          NULLIF(mant.gmv_mes, 0) * 100, 1)                    AS variacion_pct,
    g.meses_activo,
    g.total_ordenes,
    ROUND(g.total_ordenes * 1.0 / NULLIF(g.meses_activo, 0),1) AS frecuencia_mensual,
    g.ultimo_pedido,
    CAST(julianday('now') - julianday(g.ultimo_pedido) AS INT)  AS dias_sin_pedido,
    ROUND(COALESCE(m.margen_pct, 0) * 100, 1)                  AS margen_pct,
    ROUND(COALESCE(c.cartera_total, 0), 0)                     AS cartera_total,
    ROUND(COALESCE(c.cartera_vencida, 0), 0)                   AS cartera_vencida,
    ROW_NUMBER() OVER (ORDER BY g.gmv_anual DESC)              AS ranking,
    CASE
        WHEN julianday('now') - julianday(g.ultimo_pedido) > 30 THEN 'critico'
        WHEN julianday('now') - julianday(g.ultimo_pedido) > 21 THEN 'atencion'
        WHEN COALESCE(ma.gmv_mes, 0) < COALESCE(mant.gmv_mes, 0) * 0.7 THEN 'atencion'
        ELSE 'sano'
    END                                                          AS estado
FROM gmv_por_cliente g
JOIN raw_partner rp ON g.partner_id = rp.id
LEFT JOIN gmv_mes_actual ma ON g.partner_id = ma.partner_id
LEFT JOIN gmv_mes_anterior mant ON g.partner_id = mant.partner_id
LEFT JOIN margen_por_cliente m ON g.partner_id = m.partner_id
LEFT JOIN cartera_por_cliente c ON g.partner_id = c.partner_id
ORDER BY g.gmv_anual DESC
"""

# ============================================================
# VISTA 4 — Productividad por vendedor
# Responde: ¿quién vende más, quién es más rentable?
# ============================================================
VISTAS["v_vendedores"] = """
CREATE VIEW IF NOT EXISTS v_vendedores AS
WITH base AS (
    SELECT
        COALESCE(so.user_id, 0)                                 AS vendedor_id,
        SUM(so.amount_untaxed)                                   AS gmv_anual,
        COUNT(DISTINCT so.partner_id)                            AS clientes_unicos,
        COUNT(so.id)                                             AS total_ordenes,
        COUNT(DISTINCT strftime('%Y-%m', so.date_order))         AS meses_activo,
        MAX(so.date_order)                                       AS ultimo_pedido,
        AVG(so.amount_untaxed)                                   AS ticket_promedio
    FROM raw_sale_order so
    WHERE so.state IN ('sale', 'done')
      AND so.company_id = 2
      AND so.partner_id != 3
      AND so.date_order >= date('now', '-12 months')
    GROUP BY so.user_id
),
margen AS (
    SELECT
        COALESCE(so.user_id, 0)                                 AS vendedor_id,
        SUM(sol.price_subtotal)                                  AS revenue,
        SUM(COALESCE(rp.standard_price, 0) * sol.product_uom_qty)            AS costo
    FROM raw_sale_order so
    JOIN raw_sale_order_line sol ON sol.order_id = so.id
    LEFT JOIN raw_product rp ON sol.product_id = rp.id
    WHERE so.state IN ('sale', 'done')
      AND so.company_id = 2
      AND so.date_order >= date('now', '-12 months')
    GROUP BY so.user_id
),
mes_actual AS (
    SELECT
        COALESCE(user_id, 0) AS vendedor_id,
        SUM(amount_untaxed)  AS gmv_mes
    FROM raw_sale_order
    WHERE state IN ('sale', 'done')
      AND company_id = 2
      AND strftime('%Y-%m', date_order) = strftime('%Y-%m', 'now', 'localtime')
    GROUP BY user_id
)
SELECT
    b.vendedor_id,
    COALESCE(ru.name, 'Sin asignar')                            AS nombre,
    ROUND(b.gmv_anual, 0)                                       AS gmv_anual,
    ROUND(b.gmv_anual / 12, 0)                                  AS gmv_mensual_prom,
    ROUND(COALESCE(ma.gmv_mes, 0), 0)                          AS gmv_mes_actual,
    b.clientes_unicos,
    b.total_ordenes,
    ROUND(b.total_ordenes * 1.0 / NULLIF(b.clientes_unicos, 0),1) AS ordenes_por_cliente,
    ROUND(b.ticket_promedio, 0)                                 AS ticket_promedio,
    ROUND((m.revenue - m.costo) /
          NULLIF(m.revenue, 0) * 100, 1)                       AS margen_pct,
    ROUND(b.gmv_anual /
          NULLIF(b.clientes_unicos, 0), 0)                     AS gmv_por_cliente,
    b.ultimo_pedido,
    CASE
        WHEN b.vendedor_id = 0 THEN 'vacante'
        WHEN COALESCE(ru.active, 1) = 0 THEN 'inactivo'
        ELSE 'activo'
    END                                                          AS estado
FROM base b
LEFT JOIN raw_user ru ON b.vendedor_id = ru.id
LEFT JOIN mes_actual ma ON b.vendedor_id = ma.vendedor_id
LEFT JOIN margen m ON b.vendedor_id = m.vendedor_id
ORDER BY b.gmv_anual DESC
"""

# ============================================================
# VISTA 5 — Cartera por aging
# Responde: ¿cuánta plata se debe y con qué antigüedad?
# ============================================================
VISTAS["v_cartera_aging"] = """
CREATE VIEW IF NOT EXISTS v_cartera_aging AS
SELECT
    am.id,
    am.name                                                      AS factura,
    am.partner_id,
    rp.name                                                      AS cliente,
    am.invoice_date,
    am.invoice_date_due,
    CAST(julianday('now') - julianday(am.invoice_date_due)
         AS INT)                                                 AS dias_vencido,
    am.amount_total,
    am.amount_residual                                           AS saldo_pendiente,
    am.payment_state,
    CASE
        WHEN am.payment_state = 'paid'
        THEN 'Pagado'
        WHEN am.invoice_date_due IS NULL
        THEN 'Sin vencimiento'
        WHEN julianday('now') <= julianday(am.invoice_date_due)
        THEN '0-30 dias'
        WHEN julianday('now') - julianday(am.invoice_date_due) <= 15
        THEN '0-30 dias'
        WHEN julianday('now') - julianday(am.invoice_date_due) <= 30
        THEN '31-45 dias'
        WHEN julianday('now') - julianday(am.invoice_date_due) <= 45
        THEN '46-60 dias'
        WHEN julianday('now') - julianday(am.invoice_date_due) <= 75
        THEN '61-90 dias'
        ELSE '+90 dias'
    END                                                          AS bucket_aging
FROM raw_account_move am
JOIN raw_partner rp ON am.partner_id = rp.id
WHERE am.move_type = 'out_invoice'
  AND am.state = 'posted'
  AND am.amount_residual > 0
ORDER BY dias_vencido DESC
"""

# ============================================================
# VISTA 6 — Mix de categorías de producto
# Responde: ¿qué categorías se mueven más y con qué margen?
# ============================================================
VISTAS["v_categorias"] = """
CREATE VIEW IF NOT EXISTS v_categorias AS
WITH categorias AS (
    SELECT
        COALESCE(
            CASE
                WHEN UPPER(rp.name) LIKE '%WHISKY%' OR UPPER(rp.name) LIKE '%WHISKEY%'
                    OR UPPER(rp.name) LIKE '%SCOTCH%' OR UPPER(rp.name) LIKE '%BOURBON%'
                THEN 'Whisky'
                WHEN UPPER(rp.name) LIKE '%VINO%' OR UPPER(rp.name) LIKE '%WINE%'
                    OR UPPER(rp.name) LIKE '%MERLOT%' OR UPPER(rp.name) LIKE '%CHARDONNAY%'
                    OR UPPER(rp.name) LIKE '%CABERNET%' OR UPPER(rp.name) LIKE '%SAUVIGNON%'
                THEN 'Vinos'
                WHEN UPPER(rp.name) LIKE '%ESPUMANTE%' OR UPPER(rp.name) LIKE '%CHAMPAGNE%'
                    OR UPPER(rp.name) LIKE '%CHAMPA%' OR UPPER(rp.name) LIKE '%PROSECCO%'
                    OR UPPER(rp.name) LIKE '%CAVA%' OR UPPER(rp.name) LIKE '%ESPUMOSO%'
                THEN 'Espumantes'
                WHEN UPPER(rp.name) LIKE '%GIN%' OR UPPER(rp.name) LIKE '%GINEBRA%'
                THEN 'Gin'
                WHEN UPPER(rp.name) LIKE '%MEZCAL%'
                THEN 'Mezcal'
                WHEN UPPER(rp.name) LIKE '%TEQUILA%'
                THEN 'Tequila'
                WHEN UPPER(rp.name) LIKE '%VODKA%'
                THEN 'Vodka'
                WHEN UPPER(rp.name) LIKE '%RON%' OR UPPER(rp.name) LIKE '%RUM%'
                THEN 'Ron'
                WHEN UPPER(rp.name) LIKE '%AGUARDIENTE%'
                THEN 'Aguardiente'
                WHEN UPPER(rp.name) LIKE '%BRANDY%' OR UPPER(rp.name) LIKE '%COGNAC%'
                    OR UPPER(rp.name) LIKE '%COÑAC%'
                THEN 'Brandy / Cognac'
                WHEN UPPER(rp.name) LIKE '%CERVEZA%' OR UPPER(rp.name) LIKE '%BEER%'
                THEN 'Cerveza'
                WHEN UPPER(rp.name) LIKE '%APEROL%' OR UPPER(rp.name) LIKE '%CAMPARI%'
                    OR UPPER(rp.name) LIKE '%VERMOUTH%' OR UPPER(rp.name) LIKE '%VERMUT%'
                    OR UPPER(rp.name) LIKE '%APERITIVO%'
                THEN 'Aperitivos'
                ELSE 'Otros'
            END, 'Otros'
        )                                                        AS categoria,
        sol.price_subtotal                                       AS revenue,
        rp2.standard_price * sol.product_uom_qty                AS costo,
        so.date_order
    FROM raw_sale_order so
    JOIN raw_sale_order_line sol ON sol.order_id = so.id
    LEFT JOIN raw_product rp ON sol.product_id = rp.id
    LEFT JOIN raw_product rp2 ON sol.product_id = rp2.id
    WHERE so.state IN ('sale', 'done')
      AND so.company_id = 2
      AND so.date_order >= date('now', '-12 months')
)
SELECT
    categoria,
    ROUND(SUM(revenue), 0)                                      AS gmv_neto,
    ROUND(SUM(revenue) * 100.0 /
          SUM(SUM(revenue)) OVER (), 1)                         AS pct_gmv,
    ROUND((SUM(revenue) - SUM(costo)) /
          NULLIF(SUM(revenue), 0) * 100, 1)                    AS margen_pct,
    COUNT(*)                                                     AS lineas_vendidas
FROM categorias
GROUP BY categoria
ORDER BY gmv_neto DESC
"""

# ============================================================
# VISTA 7 — White space por cliente top
# Responde: ¿qué categorías del portafolio NO compra cada cliente top?
# ============================================================
VISTAS["v_white_space"] = """
CREATE VIEW IF NOT EXISTS v_white_space AS
WITH top_clientes AS (
    SELECT partner_id
    FROM raw_sale_order
    WHERE state IN ('sale', 'done') AND company_id = 2
      AND partner_id != 3
      AND date_order >= date('now', '-12 months')
    GROUP BY partner_id
    ORDER BY SUM(amount_untaxed) DESC
    LIMIT 30
),
categorias_cliente AS (
    SELECT
        so.partner_id,
        CASE
            WHEN UPPER(rp.name) LIKE '%WHISKY%' OR UPPER(rp.name) LIKE '%WHISKEY%'
                OR UPPER(rp.name) LIKE '%SCOTCH%' THEN 'Whisky'
            WHEN UPPER(rp.name) LIKE '%VINO%' OR UPPER(rp.name) LIKE '%WINE%'
                OR UPPER(rp.name) LIKE '%MERLOT%' OR UPPER(rp.name) LIKE '%CABERNET%'
            THEN 'Vinos'
            WHEN UPPER(rp.name) LIKE '%ESPUMANTE%' OR UPPER(rp.name) LIKE '%CHAMPAGNE%'
                OR UPPER(rp.name) LIKE '%PROSECCO%' THEN 'Espumantes'
            WHEN UPPER(rp.name) LIKE '%GIN%' OR UPPER(rp.name) LIKE '%GINEBRA%' THEN 'Gin'
            WHEN UPPER(rp.name) LIKE '%MEZCAL%' THEN 'Mezcal'
            WHEN UPPER(rp.name) LIKE '%TEQUILA%' THEN 'Tequila'
            WHEN UPPER(rp.name) LIKE '%VODKA%' THEN 'Vodka'
            WHEN UPPER(rp.name) LIKE '%RON%' OR UPPER(rp.name) LIKE '%RUM%' THEN 'Ron'
            ELSE NULL
        END AS categoria,
        SUM(sol.price_subtotal) AS gmv_categoria
    FROM raw_sale_order so
    JOIN raw_sale_order_line sol ON sol.order_id = so.id
    LEFT JOIN raw_product rp ON sol.product_id = rp.id
    WHERE so.partner_id IN (SELECT partner_id FROM top_clientes)
      AND so.state IN ('sale', 'done')
      AND so.company_id = 2
      AND so.date_order >= date('now', '-12 months')
      AND categoria IS NOT NULL
    GROUP BY so.partner_id, categoria
)
SELECT
    tc.partner_id,
    rp.name                                                      AS cliente,
    cc.categoria,
    ROUND(COALESCE(cc.gmv_categoria, 0), 0)                    AS gmv_categoria,
    CASE WHEN cc.gmv_categoria IS NULL THEN 1 ELSE 0 END        AS es_white_space
FROM top_clientes tc
CROSS JOIN (
    SELECT 'Whisky' AS categoria UNION ALL SELECT 'Vinos' UNION ALL
    SELECT 'Espumantes' UNION ALL SELECT 'Gin' UNION ALL
    SELECT 'Mezcal' UNION ALL SELECT 'Tequila' UNION ALL
    SELECT 'Vodka' UNION ALL SELECT 'Ron'
) todas_categorias
JOIN raw_partner rp ON tc.partner_id = rp.id
LEFT JOIN categorias_cliente cc
    ON tc.partner_id = cc.partner_id
    AND todas_categorias.categoria = cc.categoria
ORDER BY tc.partner_id, todas_categorias.categoria
"""

# ============================================================
# VISTA 8 — Alertas operativas
# Responde: ¿qué requiere intervención inmediata esta semana?
# ============================================================
VISTAS["v_alertas"] = """
CREATE VIEW IF NOT EXISTS v_alertas AS
WITH top30 AS (
    SELECT partner_id
    FROM raw_sale_order
    WHERE state IN ('sale', 'done') AND company_id = 2
      AND partner_id != 3
      AND date_order >= date('now', '-12 months')
    GROUP BY partner_id
    ORDER BY SUM(amount_untaxed) DESC
    LIMIT 30
),
ultimo_pedido AS (
    SELECT partner_id,
           MAX(date_order) AS ultimo_pedido,
           CAST(julianday('now') - julianday(MAX(date_order)) AS INT) AS dias_inactivo
    FROM raw_sale_order
    WHERE state IN ('sale', 'done') AND company_id = 2
    GROUP BY partner_id
),
cartera_critica AS (
    SELECT partner_id,
           SUM(amount_residual) AS saldo_vencido,
           MAX(CAST(julianday('now') - julianday(invoice_date_due) AS INT)) AS max_dias
    FROM raw_account_move
    WHERE move_type = 'out_invoice' AND state = 'posted'
      AND payment_state != 'paid' AND company_id = 2
      AND julianday('now') - julianday(invoice_date_due) > 30
    GROUP BY partner_id
    HAVING SUM(amount_residual) > 1000000
)
SELECT
    rp.name                                                      AS cliente,
    up.dias_inactivo,
    up.ultimo_pedido,
    COALESCE(cc.saldo_vencido, 0)                              AS cartera_vencida,
    COALESCE(cc.max_dias, 0)                                   AS max_dias_vencido,
    CASE
        WHEN t30.partner_id IS NOT NULL AND up.dias_inactivo > 21
        THEN 'rojo'
        WHEN up.dias_inactivo > 30 THEN 'rojo'
        WHEN cc.max_dias > 60 THEN 'rojo'
        WHEN up.dias_inactivo > 14 THEN 'amarillo'
        WHEN cc.max_dias > 30 THEN 'amarillo'
        ELSE 'verde'
    END                                                          AS semaforo,
    CASE
        WHEN t30.partner_id IS NOT NULL AND up.dias_inactivo > 21
        THEN 'Cliente top sin pedido hace ' || up.dias_inactivo || ' días'
        WHEN cc.max_dias > 60
        THEN 'Cartera vencida crítica: ' || cc.max_dias || ' días'
        WHEN up.dias_inactivo > 14
        THEN 'Sin pedido hace ' || up.dias_inactivo || ' días'
        ELSE 'OK'
    END                                                          AS descripcion_alerta
FROM ultimo_pedido up
JOIN raw_partner rp ON up.partner_id = rp.id
LEFT JOIN top30 t30 ON up.partner_id = t30.partner_id
LEFT JOIN cartera_critica cc ON up.partner_id = cc.partner_id
WHERE (
    up.dias_inactivo > 14
    OR cc.max_dias > 30
)
AND rp.name NOT LIKE '%Administrator%'
ORDER BY
    CASE semaforo WHEN 'rojo' THEN 1 WHEN 'amarillo' THEN 2 ELSE 3 END,
    up.dias_inactivo DESC
"""


def crear_vistas(engine):
    """
    Crea o recrea todas las vistas analíticas en SQLite.
    Llama esto desde construir_modelo.py.
    """
    with engine.connect() as conn:
        for nombre, sql in VISTAS.items():
            conn.execute(text(f"DROP VIEW IF EXISTS {nombre}"))
            conn.execute(text(sql))
            conn.commit()
            print(f"  Vista creada: {nombre}")
    print(f"\n  Total: {len(VISTAS)} vistas listas")
