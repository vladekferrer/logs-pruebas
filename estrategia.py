"""
Monitor estratégico TBS.

Mide el avance de los 6 objetivos del plan estratégico:
  1. Densificación de los 9 hoteles top (SoW 20% → 35%)
  2. Recuperación de cuentas Vacante 87
  3. Codificación de marcas estratégicas
  4. Control de cartera (< 8% vencida)
  5. Captación de clientes nuevos (≥2 por vendedor/mes)
  6. Evolución del mix premium (% GMV en categorías premium)

Diferencia con monitor.py:
  monitor.py  → estado operativo hoy (quién visitó, cuota del mes)
  estrategia.py → avance del plan a 3-6 meses (qué está funcionando)
"""
from sqlalchemy import create_engine, text
from config import config

COMPANY_ID = 2
MESES_ANALISIS = 4   # cuántos meses hacia atrás analizar tendencia

# Hoteles objetivo del plan
HOTELES_OBJETIVO = [
    "HOTEL CARIBE",
    "HOTEL SANTA CLARA",
    "CASA DON LUIS",
    "HOTEL SAN PEDRO",
    "HYATT REGENCY",
    "VOILA HOTEL",
    "HOTEL LAS ISLAS",
    "ARSENAL HOTEL",
    "HOTELES DE LA ANTIGUA",
]

# Metas del plan (ajustar según realidad)
METAS = {
    "sow_hoteles_target_pct":    35.0,   # % SoW objetivo en hoteles top
    "marcas_cobertura_target":   70.0,   # % cuentas top con marcas estratégicas
    "cartera_vencida_max":        8.0,   # % máximo de cartera vencida >30d
    "clientes_nuevos_mes":        6,     # total equipo por mes
    "pct_gmv_premium_min":       35.0,   # % mínimo GMV en categorías premium
    "cuentas_vacante_recuperar":  30,    # cuentas de vacante 87 a recuperar
}

# Categorías consideradas "premium" para el mix
CATEGORIAS_PREMIUM = [
    "Whisky", "Vinos", "Espumantes", "Gin",
    "Mezcal", "Ron", "Tequila",
]


def _engine():
    return create_engine(config.DB_URL, echo=False)


# ════════════════════════════════════════════════════════
# OBJ 1 — DENSIFICACIÓN DE HOTELES TOP
# ════════════════════════════════════════════════════════

def obj_densificacion_hoteles() -> dict:
    """
    Mide el avance del SoW en los 9 hoteles objetivo.
    Calcula tendencia mes a mes en los últimos MESES_ANALISIS meses.
    """
    engine = _engine()
    with engine.connect() as conn:

        # GMV por hotel por mes (últimos N meses)
        rows = conn.execute(text("""
            SELECT
                rp.name                                         AS hotel,
                strftime('%Y-%m', so.date_order)                AS mes,
                ROUND(SUM(so.amount_untaxed), 0)                AS gmv_tbs
            FROM raw_sale_order so
            JOIN raw_partner rp ON so.partner_id = rp.id
            WHERE so.company_id = :cid
              AND so.state IN ('sale','done')
              AND so.date_order >= date('now', :meses)
              AND (
                  UPPER(rp.name) LIKE '%HOTEL CARIBE%' OR
                  UPPER(rp.name) LIKE '%SANTA CLARA%' OR
                  UPPER(rp.name) LIKE '%DON LUIS%' OR
                  UPPER(rp.name) LIKE '%SAN PEDRO DE MAJAGUA%' OR
                  UPPER(rp.name) LIKE '%HYATT%' OR
                  UPPER(rp.name) LIKE '%VOILA%' OR
                  UPPER(rp.name) LIKE '%LAS ISLAS%' OR
                  UPPER(rp.name) LIKE '%ARSENAL HOTEL%' OR
                  UPPER(rp.name) LIKE '%ANTIGUA%'
              )
            GROUP BY rp.name, strftime('%Y-%m', so.date_order)
            ORDER BY rp.name, mes
        """), {
            "cid": COMPANY_ID,
            "meses": f"-{MESES_ANALISIS} months",
        }).mappings().fetchall()

        # Mes actual para cada hotel
        actual = conn.execute(text("""
            SELECT
                rp.name                                         AS hotel,
                ROUND(SUM(so.amount_untaxed), 0)                AS gmv_mes_actual,
                COUNT(so.id)                                    AS ordenes
            FROM raw_sale_order so
            JOIN raw_partner rp ON so.partner_id = rp.id
            WHERE so.company_id = :cid
              AND so.state IN ('sale','done')
              AND strftime('%Y-%m', so.date_order) =
                  strftime('%Y-%m', 'now', 'localtime')
              AND (
                  UPPER(rp.name) LIKE '%HOTEL CARIBE%' OR
                  UPPER(rp.name) LIKE '%SANTA CLARA%' OR
                  UPPER(rp.name) LIKE '%DON LUIS%' OR
                  UPPER(rp.name) LIKE '%SAN PEDRO DE MAJAGUA%' OR
                  UPPER(rp.name) LIKE '%HYATT%' OR
                  UPPER(rp.name) LIKE '%VOILA%' OR
                  UPPER(rp.name) LIKE '%LAS ISLAS%' OR
                  UPPER(rp.name) LIKE '%ARSENAL HOTEL%' OR
                  UPPER(rp.name) LIKE '%ANTIGUA%'
              )
            GROUP BY rp.name
        """), {"cid": COMPANY_ID}).mappings().fetchall()

    # Agrupar tendencia por hotel
    tendencias = {}
    for r in rows:
        hotel = r["hotel"]
        if hotel not in tendencias:
            tendencias[hotel] = []
        tendencias[hotel].append({"mes": r["mes"], "gmv": r["gmv_tbs"]})

    actual_map = {r["hotel"]: r for r in actual}

    resumen = []
    for hotel, meses in tendencias.items():
        gmv_actual = actual_map.get(hotel, {}).get("gmv_mes_actual", 0) or 0
        gmv_inicio = meses[0]["gmv"] if meses else 0
        gmv_promedio = sum(m["gmv"] for m in meses) / len(meses) if meses else 0

        # Tendencia: comparar último cuarto del período vs primero
        n = len(meses)
        if n >= 2:
            primera_mitad = sum(m["gmv"] for m in meses[:n//2]) / (n//2)
            segunda_mitad = sum(m["gmv"] for m in meses[n//2:]) / (n - n//2)
            tendencia_pct = (segunda_mitad - primera_mitad) / primera_mitad * 100 \
                if primera_mitad else 0
        else:
            tendencia_pct = 0

        if tendencia_pct > 10:
            estado = "creciendo"
        elif tendencia_pct < -10:
            estado = "cayendo"
        else:
            estado = "estable"

        resumen.append({
            "hotel": hotel,
            "gmv_mes_actual": gmv_actual,
            "gmv_promedio_periodo": round(gmv_promedio, 0),
            "tendencia_pct": round(tendencia_pct, 1),
            "estado": estado,
            "historial_meses": meses,
            "meta_gmv_estimada": round(gmv_promedio * 1.75, 0),
        })

    resumen.sort(key=lambda x: x["gmv_mes_actual"], reverse=True)

    hoteles_creciendo = sum(1 for h in resumen if h["estado"] == "creciendo")
    hoteles_cayendo   = sum(1 for h in resumen if h["estado"] == "cayendo")

    return {
        "objetivo": "Densificación 9 hoteles top (SoW 20% → 35%)",
        "meta": METAS["sow_hoteles_target_pct"],
        "hoteles": resumen,
        "resumen": {
            "total_hoteles":    len(resumen),
            "creciendo":        hoteles_creciendo,
            "estables":         len(resumen) - hoteles_creciendo - hoteles_cayendo,
            "cayendo":          hoteles_cayendo,
            "gmv_total_actual": sum(h["gmv_mes_actual"] for h in resumen),
        },
        "alerta": hoteles_cayendo >= 2,
    }


# ════════════════════════════════════════════════════════
# OBJ 2 — RECUPERACIÓN VACANTE 87
# ════════════════════════════════════════════════════════

def obj_recuperacion_vacante() -> dict:
    """
    Mide cuántas de las 46 cuentas huérfanas de Vacante 87
    están siendo recuperadas (tienen pedido en los últimos 30 días).
    """
    engine = _engine()
    with engine.connect() as conn:

        # Cuentas que tuvieron actividad con Vacante 87 en 2025
        vacante_cuentas = conn.execute(text("""
            SELECT DISTINCT so.partner_id, rp.name AS cliente,
                   SUM(so.amount_untaxed) AS gmv_historico,
                   MAX(so.date_order) AS ultimo_pedido
            FROM raw_sale_order so
            JOIN raw_partner rp ON so.partner_id = rp.id
            LEFT JOIN raw_user ru ON so.user_id = ru.id
            WHERE so.company_id = :cid
              AND so.state IN ('sale','done')
              AND (ru.name LIKE '%VACANTE%'
                   OR ru.name LIKE '%87%'
                   OR so.user_id IS NULL)
              AND so.date_order >= '2025-01-01'
            GROUP BY so.partner_id
            ORDER BY gmv_historico DESC
        """), {"cid": COMPANY_ID}).mappings().fetchall()

        total_cuentas = len(vacante_cuentas)

        # Cuáles tienen pedido reciente (últimos 60 días, cualquier vendedor)
        ids = [r["partner_id"] for r in vacante_cuentas]
        recuperadas = 0
        perdidas = 0
        if ids:
            ids_str = ",".join(str(i) for i in ids)
            recientes = conn.execute(text(f"""
                SELECT DISTINCT partner_id
                FROM raw_sale_order
                WHERE company_id = :cid
                  AND state IN ('sale','done')
                  AND date_order >= date('now', '-60 days')
                  AND partner_id IN ({ids_str})
            """), {"cid": COMPANY_ID}).fetchall()
            recuperadas = len(recientes)
            perdidas = total_cuentas - recuperadas

    gmv_en_riesgo = sum(
        r["gmv_historico"] for r in vacante_cuentas
    ) / 12  # promedio mensual

    return {
        "objetivo": "Recuperar 30 de 46 cuentas Vacante 87",
        "meta": METAS["cuentas_vacante_recuperar"],
        "total_cuentas_vacante": total_cuentas,
        "recuperadas_60d": recuperadas,
        "en_riesgo": perdidas,
        "pct_recuperado": round(recuperadas / total_cuentas * 100, 1)
                          if total_cuentas else 0,
        "gmv_mensual_en_riesgo": round(gmv_en_riesgo, 0),
        "top_cuentas_perdidas": [
            {"cliente": r["cliente"],
             "gmv_mensual": round(r["gmv_historico"] / 12, 0),
             "ultimo_pedido": str(r["ultimo_pedido"])}
            for r in vacante_cuentas[:5]
        ],
        "alerta": perdidas > 20,
    }


# ════════════════════════════════════════════════════════
# OBJ 3 — CODIFICACIÓN DE MARCAS
# ════════════════════════════════════════════════════════

def obj_codificacion_marcas() -> dict:
    """
    Mide el avance de codificación de marcas estratégicas
    en el top 30 de cuentas.
    """
    from agente.monitor import codificacion_marcas
    marcas = codificacion_marcas(top_n=30)

    con_cobertura_alta = sum(1 for m in marcas if m["cobertura_pct"] >= 70)
    cobertura_promedio = sum(m["cobertura_pct"] for m in marcas) / len(marcas) \
                         if marcas else 0

    return {
        "objetivo": "Codificar marcas estratégicas en top 30 cuentas",
        "meta": METAS["marcas_cobertura_target"],
        "cobertura_promedio_pct": round(cobertura_promedio, 1),
        "cuentas_sobre_70pct": con_cobertura_alta,
        "total_cuentas": len(marcas),
        "cuentas_con_gaps": [
            m for m in marcas if m["marcas_faltantes"]
        ][:8],
        "alerta": cobertura_promedio < 50,
    }


# ════════════════════════════════════════════════════════
# OBJ 4 — CONTROL DE CARTERA
# ════════════════════════════════════════════════════════

def obj_cartera() -> dict:
    """
    Evolución mensual del % de cartera vencida >30d.
    Identifica si la tendencia es de mejora o deterioro.
    """
    engine = _engine()
    with engine.connect() as conn:

        cartera_actual = conn.execute(text("""
            SELECT
                ROUND(SUM(amount_residual), 0)         AS cartera_total,
                ROUND(SUM(CASE
                    WHEN julianday('now') -
                         julianday(invoice_date_due) > 30
                    THEN amount_residual ELSE 0
                END), 0)                               AS vencida_30d,
                ROUND(SUM(CASE
                    WHEN julianday('now') -
                         julianday(invoice_date_due) > 60
                    THEN amount_residual ELSE 0
                END), 0)                               AS vencida_60d,
                ROUND(SUM(CASE
                    WHEN julianday('now') -
                         julianday(invoice_date_due) > 90
                    THEN amount_residual ELSE 0
                END), 0)                               AS vencida_90d
            FROM raw_account_move
            WHERE move_type = 'out_invoice'
              AND state     = 'posted'
              AND amount_residual > 0
        """)).mappings().first()

        total = cartera_actual["cartera_total"] or 1
        pct_30 = (cartera_actual["vencida_30d"] or 0) / total * 100
        pct_60 = (cartera_actual["vencida_60d"] or 0) / total * 100
        pct_90 = (cartera_actual["vencida_90d"] or 0) / total * 100

    return {
        "objetivo": "Mantener cartera vencida <8%",
        "meta": METAS["cartera_vencida_max"],
        "cartera_total": cartera_actual["cartera_total"],
        "vencida_30d": cartera_actual["vencida_30d"],
        "vencida_60d": cartera_actual["vencida_60d"],
        "vencida_90d": cartera_actual["vencida_90d"],
        "pct_vencida_30d": round(pct_30, 1),
        "pct_vencida_60d": round(pct_60, 1),
        "pct_vencida_90d": round(pct_90, 1),
        "estado": "critico" if pct_30 > 15
                  else "alerta" if pct_30 > 8
                  else "sano",
        "alerta": pct_30 > 8,
    }


# ════════════════════════════════════════════════════════
# OBJ 5 — CAPTACIÓN DE CLIENTES NUEVOS
# ════════════════════════════════════════════════════════

def obj_clientes_nuevos() -> dict:
    """
    Captación mensual de clientes nuevos vs meta del plan.
    Tendencia de los últimos meses.
    """
    engine = _engine()
    with engine.connect() as conn:

        mensual = conn.execute(text("""
            SELECT
                strftime('%Y-%m', so.date_order) AS mes,
                COUNT(DISTINCT so.partner_id)     AS clientes_nuevos
            FROM raw_sale_order so
            WHERE so.company_id = :cid
              AND so.state IN ('sale','done')
              AND so.partner_id != 3
              AND so.date_order >= date('now', '-5 months')
              AND so.partner_id NOT IN (
                  SELECT DISTINCT partner_id
                  FROM raw_sale_order
                  WHERE company_id = :cid
                    AND state IN ('sale','done')
                    AND date_order < date(so.date_order, 'start of month')
              )
            GROUP BY strftime('%Y-%m', so.date_order)
            ORDER BY mes DESC
        """), {"cid": COMPANY_ID}).mappings().fetchall()

    meses = [dict(r) for r in mensual]
    promedio = sum(m["clientes_nuevos"] for m in meses) / len(meses) \
               if meses else 0
    mes_actual = meses[0]["clientes_nuevos"] if meses else 0

    return {
        "objetivo": f"≥{METAS['clientes_nuevos_mes']} clientes nuevos por mes (equipo)",
        "meta_mensual": METAS["clientes_nuevos_mes"],
        "mes_actual": mes_actual,
        "promedio_ultimos_meses": round(promedio, 1),
        "tendencia_meses": meses,
        "cumpliendo_meta": mes_actual >= METAS["clientes_nuevos_mes"],
        "alerta": mes_actual < METAS["clientes_nuevos_mes"] * 0.5,
    }


# ════════════════════════════════════════════════════════
# OBJ 6 — MIX PREMIUM
# ════════════════════════════════════════════════════════

def obj_mix_premium() -> dict:
    """
    Evolución mensual del % de GMV en categorías premium.
    Detecta si el negocio se está commoditizando.
    """
    engine = _engine()
    cat_cases = " ".join([
        f"WHEN UPPER(COALESCE(rpt.name, rp.name, '')) LIKE '%{c.upper()}%' THEN '{c}'"
        for c in CATEGORIAS_PREMIUM
    ])

    with engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT
                strftime('%Y-%m', so.date_order)     AS mes,
                ROUND(SUM(sol.price_subtotal), 0)     AS gmv_total,
                ROUND(SUM(CASE
                    WHEN (CASE {cat_cases} ELSE NULL END) IS NOT NULL
                    THEN sol.price_subtotal ELSE 0
                END), 0)                              AS gmv_premium
            FROM raw_sale_order so
            JOIN raw_sale_order_line sol       ON sol.order_id = so.id
            LEFT JOIN raw_product rp           ON sol.product_id = rp.id
            LEFT JOIN raw_product_template rpt ON rp.product_tmpl_id = rpt.id
            WHERE so.company_id = :cid
              AND so.state IN ('sale','done')
              AND so.date_order >= date('now', '-5 months')
            GROUP BY strftime('%Y-%m', so.date_order)
            ORDER BY mes DESC
        """), {"cid": COMPANY_ID}).mappings().fetchall()

    meses = []
    for r in rows:
        pct = (r["gmv_premium"] or 0) / max(r["gmv_total"] or 1, 1) * 100
        meses.append({
            "mes": r["mes"],
            "gmv_total": r["gmv_total"],
            "gmv_premium": r["gmv_premium"],
            "pct_premium": round(pct, 1),
        })

    pct_actual = meses[0]["pct_premium"] if meses else 0
    tendencia = meses[0]["pct_premium"] - meses[-1]["pct_premium"] \
                if len(meses) >= 2 else 0

    return {
        "objetivo": f"≥{METAS['pct_gmv_premium_min']}% GMV en categorías premium",
        "meta": METAS["pct_gmv_premium_min"],
        "pct_actual": pct_actual,
        "tendencia_ppts": round(tendencia, 1),
        "tendencia_meses": meses,
        "estado": "sano" if pct_actual >= METAS["pct_gmv_premium_min"]
                  else "alerta" if pct_actual >= METAS["pct_gmv_premium_min"] * 0.8
                  else "critico",
        "alerta": tendencia < -3,
    }


# ════════════════════════════════════════════════════════
# SNAPSHOT ESTRATÉGICO COMPLETO
# ════════════════════════════════════════════════════════

def snapshot_estrategico() -> dict:
    """
    Retorna el estado de los 6 objetivos estratégicos.
    Es la entrada principal para el análisis del LLM.
    """
    return {
        "hoteles":    obj_densificacion_hoteles(),
        "vacante":    obj_recuperacion_vacante(),
        "marcas":     obj_codificacion_marcas(),
        "cartera":    obj_cartera(),
        "nuevos":     obj_clientes_nuevos(),
        "mix":        obj_mix_premium(),
    }
