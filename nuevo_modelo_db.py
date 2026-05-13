"""
Schema completo de la base analítica TBS.

Tablas crudas de Odoo (raw_*):
  raw_sale_order, raw_sale_order_line, raw_account_move,
  raw_partner, raw_product, raw_product_template,
  raw_user, raw_product_category, raw_partner_industry

Tablas analíticas (dim_*, fct_*):
  dim_cliente, dim_vendedor, dim_producto
  fct_orden, fct_orden_linea, fct_cartera

Tablas del agente supervisor:
  visitas_vendedor, compromisos_vendedor,
  cuota_mensual, briefing_diario

Tablas de sistema:
  log_extraccion, llm_insights
"""
from sqlalchemy import (
    create_engine, Column, Integer, String, Float,
    DateTime, Date, Boolean, ForeignKey, Text, MetaData,
)
from sqlalchemy.orm import declarative_base, sessionmaker
from config import config

Base = declarative_base()
metadata = MetaData()


# ════════════════════════════════════════════════════════
# TABLAS CRUDAS DE ODOO (raw_*)
# ════════════════════════════════════════════════════════

class RawSaleOrder(Base):
    """Espejo crudo de sale.order de Odoo."""
    __tablename__ = "raw_sale_order"

    id             = Column(Integer, primary_key=True)
    name           = Column(String(64), index=True)
    date_order     = Column(DateTime, index=True)
    partner_id     = Column(Integer, index=True)
    user_id        = Column(Integer, index=True)
    state          = Column(String(32))
    invoice_status = Column(String(32))
    amount_untaxed = Column(Float)
    amount_tax     = Column(Float)
    amount_total   = Column(Float)
    company_id     = Column(Integer)
    extracted_at   = Column(DateTime)


class RawSaleOrderLine(Base):
    __tablename__ = "raw_sale_order_line"

    id               = Column(Integer, primary_key=True)
    order_id         = Column(Integer,
                              ForeignKey("raw_sale_order.id"), index=True)
    product_id       = Column(Integer, index=True)
    product_uom_qty  = Column(Float)
    price_unit       = Column(Float)
    price_subtotal   = Column(Float)
    price_total      = Column(Float)
    discount         = Column(Float)
    extracted_at     = Column(DateTime)


class RawAccountMove(Base):
    """Facturas y notas crédito."""
    __tablename__ = "raw_account_move"

    id               = Column(Integer, primary_key=True)
    name             = Column(String(64), index=True)
    partner_id       = Column(Integer, index=True)
    invoice_date     = Column(Date, index=True)
    invoice_date_due = Column(Date, index=True)
    move_type        = Column(String(32))
    state            = Column(String(32))
    payment_state    = Column(String(32), index=True)
    amount_untaxed   = Column(Float)
    amount_tax       = Column(Float)
    amount_total     = Column(Float)
    amount_residual  = Column(Float)
    invoice_origin   = Column(String(64))
    company_id       = Column(Integer)
    extracted_at     = Column(DateTime)


class RawPartner(Base):
    __tablename__ = "raw_partner"

    id            = Column(Integer, primary_key=True)
    name          = Column(String(256), index=True)
    is_company    = Column(Boolean)
    customer_rank = Column(Integer)
    supplier_rank = Column(Integer)
    city          = Column(String(128))
    state_id      = Column(Integer)
    country_id    = Column(Integer)
    street        = Column(String(256))
    phone         = Column(String(64))
    email         = Column(String(128))
    create_date   = Column(DateTime)
    industry_id   = Column(Integer, index=True)
    extracted_at  = Column(DateTime)


class RawPartnerIndustry(Base):
    """Sectores / industrias (campo Sector en Odoo)."""
    __tablename__ = "raw_partner_industry"

    id           = Column(Integer, primary_key=True)
    name         = Column(String(128))
    full_name    = Column(String(256))
    extracted_at = Column(DateTime)


class RawProduct(Base):
    """
    Variantes de producto (product.product).
    product_tmpl_id enlaza con RawProductTemplate para obtener el costo real.
    """
    __tablename__ = "raw_product"

    id              = Column(Integer, primary_key=True)
    name            = Column(String(256))
    default_code    = Column(String(64), index=True)
    barcode         = Column(String(64))
    list_price      = Column(Float)
    standard_price  = Column(Float)
    product_tmpl_id = Column(Integer, index=True)
    categ_id        = Column(Integer, index=True)
    type            = Column(String(32))
    active          = Column(Boolean)
    extracted_at    = Column(DateTime)


class RawProductTemplate(Base):
    """
    Plantilla de producto (product.template).
    El costo real (standard_price) vive aquí con contexto force_company=2.
    """
    __tablename__ = "raw_product_template"

    id             = Column(Integer, primary_key=True)
    name           = Column(String(256))
    standard_price = Column(Float)
    categ_id       = Column(Integer, index=True)
    active         = Column(Boolean)
    company_id     = Column(Integer, index=True)
    extracted_at   = Column(DateTime)


class RawUser(Base):
    """Vendedores (res.users en Odoo)."""
    __tablename__ = "raw_user"

    id           = Column(Integer, primary_key=True)
    name         = Column(String(128), index=True)
    login        = Column(String(128))
    active       = Column(Boolean)
    extracted_at = Column(DateTime)


class RawProductCategory(Base):
    __tablename__ = "raw_product_category"

    id           = Column(Integer, primary_key=True)
    name         = Column(String(128))
    parent_id    = Column(Integer)
    extracted_at = Column(DateTime)


# ════════════════════════════════════════════════════════
# TABLAS ANALÍTICAS (dim_*, fct_*)
# ════════════════════════════════════════════════════════

class DimCliente(Base):
    """Dimensión de cliente con segmentación HORECA."""
    __tablename__ = "dim_cliente"

    id_cliente          = Column(Integer, primary_key=True)
    nombre              = Column(String(256))
    ciudad              = Column(String(128))
    tipo_horeca         = Column(String(64))
    es_top30            = Column(Boolean, default=False)
    es_hotel_top        = Column(Boolean, default=False)
    fecha_primer_pedido = Column(Date)
    fecha_ultimo_pedido = Column(Date)
    estado_actividad    = Column(String(32))


class DimVendedor(Base):
    __tablename__ = "dim_vendedor"

    id_vendedor              = Column(Integer, primary_key=True)
    nombre                   = Column(String(128))
    activo                   = Column(Boolean)
    costo_mensual_estimado   = Column(Float)


class DimProducto(Base):
    __tablename__ = "dim_producto"

    id_producto      = Column(Integer, primary_key=True)
    nombre           = Column(String(256))
    codigo_sku       = Column(String(64))
    categoria_general = Column(String(64))
    es_premium       = Column(Boolean, default=False)
    margen_target    = Column(Float)


class FctOrden(Base):
    """Tabla de hechos a nivel de orden de venta."""
    __tablename__ = "fct_orden"

    id_orden      = Column(Integer, primary_key=True)
    referencia    = Column(String(64))
    fecha         = Column(DateTime, index=True)
    id_cliente    = Column(Integer,
                           ForeignKey("dim_cliente.id_cliente"), index=True)
    id_vendedor   = Column(Integer,
                           ForeignKey("dim_vendedor.id_vendedor"), index=True)
    monto_neto    = Column(Float)
    monto_impuesto = Column(Float)
    monto_total   = Column(Float)
    estado        = Column(String(32))
    estado_factura = Column(String(32))


class FctOrdenLinea(Base):
    __tablename__ = "fct_orden_linea"

    id_linea       = Column(Integer, primary_key=True)
    id_orden       = Column(Integer,
                            ForeignKey("fct_orden.id_orden"), index=True)
    id_producto    = Column(Integer,
                            ForeignKey("dim_producto.id_producto"), index=True)
    cantidad       = Column(Float)
    precio_unitario = Column(Float)
    descuento      = Column(Float)
    subtotal       = Column(Float)
    total          = Column(Float)


class FctCartera(Base):
    """Snapshot de cartera por factura."""
    __tablename__ = "fct_cartera"

    id_factura        = Column(Integer, primary_key=True)
    referencia        = Column(String(64))
    id_cliente        = Column(Integer,
                               ForeignKey("dim_cliente.id_cliente"), index=True)
    fecha_factura     = Column(Date)
    fecha_vencimiento = Column(Date)
    monto_total       = Column(Float)
    monto_pendiente   = Column(Float)
    estado_pago       = Column(String(32), index=True)
    dias_vencido      = Column(Integer)
    bucket_aging      = Column(String(16))


# ════════════════════════════════════════════════════════
# TABLAS DEL AGENTE SUPERVISOR
# ════════════════════════════════════════════════════════

class VisitaVendedor(Base):
    """
    Registro manual de visitas del equipo comercial.
    El director o el vendedor las registran desde el dashboard del agente.
    """
    __tablename__ = "visitas_vendedor"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    vendedor     = Column(String(128), index=True)
    cliente      = Column(String(256))
    partner_id   = Column(Integer, index=True)
    fecha        = Column(Date, index=True)
    tipo         = Column(String(32))    # presencial | llamada | whatsapp
    resultado    = Column(String(512))   # resumen de qué pasó
    compromiso   = Column(Text)          # qué prometió el cliente o vendedor
    monto_pedido = Column(Float)         # si generó pedido, cuánto
    created_at   = Column(DateTime)


class CompromisoVendedor(Base):
    """
    Compromisos del equipo: clientes a contactar, pedidos a cerrar,
    cuentas a recuperar. El director los asigna, el sistema los sigue.
    """
    __tablename__ = "compromisos_vendedor"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    vendedor         = Column(String(128), index=True)
    cliente          = Column(String(256))
    partner_id       = Column(Integer)
    descripcion      = Column(Text)
    tipo             = Column(String(32))   # visita | pedido | cartera | nuevo_cliente
    fecha_compromiso = Column(Date, index=True)
    estado           = Column(String(16), default="pendiente")
                                            # pendiente | cumplido | vencido
    resultado        = Column(Text)
    created_at       = Column(DateTime)
    closed_at        = Column(DateTime)


class CuotaMensual(Base):
    """
    Cuotas por vendedor por mes.
    Permite registrar historial de cambios sin tocar agente/cuotas.py.
    """
    __tablename__ = "cuota_mensual"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    vendedor        = Column(String(128), index=True)
    mes             = Column(String(7), index=True)    # YYYY-MM
    gmv_mensual     = Column(Float)
    cuentas_activas = Column(Integer)
    clientes_nuevos = Column(Integer)
    cartera_max_pct = Column(Float)
    visitas_semana  = Column(Integer)
    created_at      = Column(DateTime)


class BriefingDiario(Base):
    """
    Historial de briefings generados por el agente supervisor.
    Permite ver la evolución del equipo día a día.
    """
    __tablename__ = "briefing_diario"

    id        = Column(Integer, primary_key=True, autoincrement=True)
    fecha     = Column(DateTime, index=True)
    contenido = Column(Text)
    mensajes  = Column(Text)    # JSON: {nombre_vendedor: mensaje_whatsapp}
    modelo    = Column(String(64))


# ════════════════════════════════════════════════════════
# TABLAS DE SISTEMA
# ════════════════════════════════════════════════════════

class LlmInsight(Base):
    """Historial de insights generados por Claude para el director."""
    __tablename__ = "llm_insights"

    id        = Column(Integer, primary_key=True, autoincrement=True)
    fecha     = Column(DateTime, index=True)
    tipo      = Column(String(32))    # diario | semanal | alerta
    contenido = Column(Text)
    modelo    = Column(String(64))


class LogExtraccion(Base):
    """Bitácora de cada corrida de extracción desde Odoo."""
    __tablename__ = "log_extraccion"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    timestamp    = Column(DateTime)
    tipo         = Column(String(32))
    modelo       = Column(String(64))
    registros    = Column(Integer)
    duracion_seg = Column(Float)
    estado       = Column(String(16))
    mensaje      = Column(Text)


# ════════════════════════════════════════════════════════
# FUNCIONES DE UTILIDAD
# ════════════════════════════════════════════════════════

def crear_db():
    """
    Crea todas las tablas si no existen.
    Es idempotente: seguro correr múltiples veces.
    Llamar desde construir_modelo.py o extraer_inicial.py.
    """
    engine = create_engine(config.DB_URL, echo=False)
    Base.metadata.create_all(engine)
    return engine


def get_session():
    engine = create_engine(config.DB_URL, echo=False)
    Session = sessionmaker(bind=engine)
    return Session()
