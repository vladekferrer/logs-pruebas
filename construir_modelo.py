"""
Construir modelo analítico TBS - versión limpia.

La clasificación de clientes viene exclusivamente del campo
'Sector' (industry_id) de res.partner en Odoo.

Para clasificar un cliente:
  1. Abrir Odoo → Contactos → buscar el cliente → campo Sector
  2. Guardar
  3. Correr: python -m scripts.extraer_incremental
  4. Correr: python -m scripts.construir_modelo
  5. El dashboard lo refleja inmediatamente

Uso:
    python -m scripts.construir_modelo
"""
import logging
import sys
from datetime import datetime
from sqlalchemy import create_engine, text

from config import config
from analisis.vistas import crear_vistas


def configurar_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(
                f"logs/modelo_{datetime.now():%Y%m%d_%H%M}.log"
            ),
            logging.StreamHandler(sys.stdout),
        ],
    )


def verificar_datos_base(engine):
    with engine.connect() as conn:
        ordenes = conn.execute(text(
            "SELECT COUNT(*) FROM raw_sale_order "
            "WHERE company_id = 2 AND state IN ('sale','done')"
        )).scalar()
        clientes = conn.execute(text(
            "SELECT COUNT(*) FROM raw_partner WHERE customer_rank > 0"
        )).scalar()
        productos = conn.execute(text(
            "SELECT COUNT(*) FROM raw_product"
        )).scalar()

    print(f"  Órdenes TBS en base:  {ordenes:,}")
    print(f"  Clientes en base:     {clientes:,}")
    print(f"  Productos en base:    {productos:,}")

    if ordenes < 100:
        raise RuntimeError(
            f"Solo {ordenes} órdenes. "
            f"Ejecuta extraer_inicial.py primero."
        )
    return ordenes


def construir_dim_cliente(engine):
    """
    Construye dim_cliente usando el campo Sector de Odoo (industry_id).
    Clientes sin sector quedan como 'Sin clasificar'.
    """
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM dim_cliente"))
        conn.execute(text("""
            INSERT INTO dim_cliente (
                id_cliente, nombre, ciudad,
                tipo_horeca, es_top30, es_hotel_top,
                fecha_primer_pedido, fecha_ultimo_pedido,
                estado_actividad
            )
            WITH ranking AS (
                SELECT
                    so.partner_id,
                    SUM(so.amount_untaxed)                           AS gmv_anual,
                    ROW_NUMBER() OVER (
                        ORDER BY SUM(so.amount_untaxed) DESC
                    )                                                AS rk
                FROM raw_sale_order so
                WHERE so.state IN ('sale', 'done')
                  AND so.company_id = 2
                  AND so.partner_id != 3
                  AND so.date_order >= date('now', '-12 months')
                GROUP BY so.partner_id
            )
            SELECT
                rp.id,
                rp.name,
                COALESCE(rp.city, 'Cartagena'),
                COALESCE(rpi.name, 'Sin clasificar'),
                CASE WHEN r.rk <= 30 THEN 1 ELSE 0 END,
                CASE WHEN r.rk <= 9
                     AND rpi.name LIKE '%Hotel%'
                     THEN 1 ELSE 0 END,
                (SELECT MIN(date_order)
                 FROM raw_sale_order
                 WHERE partner_id = rp.id
                   AND state IN ('sale','done')
                   AND company_id = 2),
                (SELECT MAX(date_order)
                 FROM raw_sale_order
                 WHERE partner_id = rp.id
                   AND state IN ('sale','done')
                   AND company_id = 2),
                CASE
                    WHEN julianday('now') - julianday(
                        (SELECT MAX(date_order)
                         FROM raw_sale_order
                         WHERE partner_id = rp.id
                           AND state IN ('sale','done')
                           AND company_id = 2)
                    ) <= 30  THEN 'activo'
                    WHEN julianday('now') - julianday(
                        (SELECT MAX(date_order)
                         FROM raw_sale_order
                         WHERE partner_id = rp.id
                           AND state IN ('sale','done')
                           AND company_id = 2)
                    ) <= 90  THEN 'inactivo_reciente'
                    ELSE 'inactivo'
                END
            FROM raw_partner rp
            JOIN ranking r ON rp.id = r.partner_id
            LEFT JOIN raw_partner_industry rpi ON rp.industry_id = rpi.id
            WHERE rp.customer_rank > 0
              AND rp.name NOT LIKE '%Administrator%'
        """))
        conn.commit()

        total = conn.execute(
            text("SELECT COUNT(*) FROM dim_cliente")
        ).scalar()
        sin_sector = conn.execute(text(
            "SELECT COUNT(*) FROM dim_cliente WHERE tipo_horeca = 'Sin clasificar'"
        )).scalar()

    print(f"  dim_cliente: {total} clientes cargados")
    print(f"  Sin clasificar en Odoo: {sin_sector} (actualizar campo Sector en Odoo)")


def construir_dim_vendedor(engine):
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM dim_vendedor"))
        conn.execute(text("""
            INSERT INTO dim_vendedor (
                id_vendedor, nombre, activo, costo_mensual_estimado
            )
            SELECT DISTINCT
                COALESCE(so.user_id, 0),
                COALESCE(ru.name, 'Sin asignar (Vacante)'),
                COALESCE(ru.active, 0),
                0.0
            FROM raw_sale_order so
            LEFT JOIN raw_user ru ON so.user_id = ru.id
            WHERE so.company_id = 2
        """))
        conn.commit()
        total = conn.execute(
            text("SELECT COUNT(*) FROM dim_vendedor")
        ).scalar()
    print(f"  dim_vendedor: {total} vendedores cargados")


def construir_fct_orden(engine):
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM fct_orden"))
        conn.execute(text("""
            INSERT INTO fct_orden (
                id_orden, referencia, fecha,
                id_cliente, id_vendedor,
                monto_neto, monto_impuesto, monto_total,
                estado, estado_factura
            )
            SELECT
                so.id, so.name, so.date_order,
                so.partner_id, COALESCE(so.user_id, 0),
                so.amount_untaxed, so.amount_tax, so.amount_total,
                so.state, so.invoice_status
            FROM raw_sale_order so
            WHERE so.state IN ('sale', 'done')
              AND so.company_id = 2
              AND so.partner_id != 3
        """))
        conn.commit()
        total = conn.execute(
            text("SELECT COUNT(*) FROM fct_orden")
        ).scalar()
    print(f"  fct_orden: {total:,} órdenes cargadas")


def construir_fct_cartera(engine):
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM fct_cartera"))
        conn.execute(text("""
            INSERT INTO fct_cartera (
                id_factura, referencia, id_cliente,
                fecha_factura, fecha_vencimiento,
                monto_total, monto_pendiente,
                estado_pago, dias_vencido, bucket_aging
            )
            SELECT
                am.id, am.name, am.partner_id,
                am.invoice_date, am.invoice_date_due,
                am.amount_total, am.amount_residual,
                am.payment_state,
                CAST(julianday('now') - julianday(
                    COALESCE(am.invoice_date_due, am.invoice_date)
                ) AS INT),
                CASE
                    WHEN am.payment_state = 'paid'
                        THEN 'Pagado'
                    WHEN am.invoice_date_due IS NULL
                        THEN 'Sin vencimiento'
                    WHEN julianday('now') <=
                         julianday(am.invoice_date_due)
                        THEN '0-30 dias'
                    WHEN julianday('now') -
                         julianday(am.invoice_date_due) <= 15
                        THEN '0-30 dias'
                    WHEN julianday('now') -
                         julianday(am.invoice_date_due) <= 30
                        THEN '31-45 dias'
                    WHEN julianday('now') -
                         julianday(am.invoice_date_due) <= 45
                        THEN '46-60 dias'
                    WHEN julianday('now') -
                         julianday(am.invoice_date_due) <= 75
                        THEN '61-90 dias'
                    ELSE '+90 dias'
                END
            FROM raw_account_move am
            WHERE am.move_type = 'out_invoice'
              AND am.state = 'posted'
              AND am.company_id = 2
        """))
        conn.commit()
        total = conn.execute(
            text("SELECT COUNT(*) FROM fct_cartera")
        ).scalar()
    print(f"  fct_cartera: {total:,} facturas cargadas")


def main():
    configurar_logging()
    logger = logging.getLogger("construir_modelo")
    logger.info("=" * 60)
    logger.info("CONSTRUYENDO MODELO ANALÍTICO TBS")
    logger.info("=" * 60)
    inicio = datetime.now()

    engine = create_engine(config.DB_URL, echo=False)

    print("\n1. Verificando datos base...")
    try:
        verificar_datos_base(engine)
    except RuntimeError as e:
        logger.error(str(e))
        sys.exit(1)

    print("\n2. Construyendo tablas dimensionales...")
    construir_dim_cliente(engine)
    construir_dim_vendedor(engine)

    print("\n3. Construyendo tablas de hechos...")
    construir_fct_orden(engine)
    construir_fct_cartera(engine)

    print("\n4. Creando vistas analíticas...")
    crear_vistas(engine)

    duracion = (datetime.now() - inicio).total_seconds()
    print(f"\n{'=' * 60}")
    print(f"MODELO CONSTRUIDO en {duracion:.1f}s")
    print(f"{'=' * 60}")
    print("\nArrancar el API:")
    print("  uvicorn api.main:app --reload --port 8000")
    print("\nDashboard en:")
    print("  http://localhost:8000\n")


if __name__ == "__main__":
    main()
