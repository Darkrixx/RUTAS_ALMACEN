#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import xmlrpc.client
from datetime import datetime, timedelta
from collections import defaultdict
import os
from flask import Flask, jsonify

# Parámetros de conexión
URL = 'https://erp.snackselvalle.com'
DB  = 'snackselvalle_fc0268f0'
USER= 'josemiruiz@snackselvalle.com'
PWD = '997523cee8dc70f78df1173b4507d994e0fdfd10'

app = Flask(__name__)

def connect():
    """Establece conexión con Odoo y devuelve uid y proxy de modelos."""
    print("🔄 Conectando con Odoo...")
    common = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/common")
    uid = common.authenticate(DB, USER, PWD, {})
    if not uid:
        raise Exception("Autenticación fallida en Odoo")
    models = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/object")
    print("✅ Conectado correctamente")
    return uid, models


def get_no_autoplan_ruta_partners(models, uid):
    """Devuelve partners que NO tienen la etiqueta 'Autoplan' y SÍ tienen 'RUTA' en método de envío"""
    print("🔍 Buscando partners que NO tienen la etiqueta 'Autoplan' y SÍ tienen 'RUTA' en método de envío...")
    domain = [
        ('category_id.name', 'not ilike', 'Autoplan'),
        ('property_delivery_carrier_id.name', 'ilike', 'RUTA')
    ]
    partner_ids = models.execute_kw(DB, uid, PWD,
        'res.partner', 'search', [domain])
    partners = models.execute_kw(DB, uid, PWD,
        'res.partner', 'read', [partner_ids], {'fields': ['id', 'name', 'complete_name', 'category_id', 'property_delivery_carrier_id']})
    # Filtrar los que no contienen 'mercadona' en complete_name
    partners_filtrados = [
        p for p in partners
        if 'mercadona' not in p.get('complete_name', '').lower()
    ]
    print(f"✅ Encontrados {len(partners_filtrados)} partners que NO tienen la etiqueta 'Autoplan' y SÍ tienen 'RUTA' en método de envío")
    return partners_filtrados


def get_pending_orders(models, uid, partner_ids):
    print("🔍 Obteniendo pedidos pending/partial/started (hoy y próximos 4 días)...")
    today = datetime.today().strftime('%Y-%m-%d')
    tomorrow = (datetime.today() + timedelta(days=1)).strftime('%Y-%m-%d')
    day_after = (datetime.today() + timedelta(days=2)).strftime('%Y-%m-%d')
    day_after_2 = (datetime.today() + timedelta(days=3)).strftime('%Y-%m-%d')
    day_after_3 = (datetime.today() + timedelta(days=4)).strftime('%Y-%m-%d')
    domain = [
        ('partner_id', 'in', partner_ids),
        ('delivery_status', 'in', ['pending', 'partial', 'started']),
        ('commitment_date', '>=', today),
        ('commitment_date', '<=', day_after_3)
    ]
    # Añadimos product_id y delivery_status para evitar búsquedas posteriores
    fields = ['id', 'name', 'partner_id', 'commitment_date', 'order_line', 'delivery_status']
    orders = models.execute_kw(DB, uid, PWD,
        'sale.order', 'search_read', [domain], {'fields': fields})
    print(f"✅ Encontrados {len(orders)} pedidos AutoplanES")
    return orders


def get_order_lines(models, uid, order_ids):
    print(f"🔍 Leyendo líneas de {len(order_ids)} pedidos...")
    domain = [('order_id', 'in', order_ids), ('product_id', '!=', False)]
    # Añadimos más campos para evitar búsquedas adicionales, incluyendo qty_delivered
    fields = ['order_id', 'product_id', 'name', 'product_uom_qty', 'product_id', 'qty_delivered']
    lines = models.execute_kw(DB, uid, PWD,
        'sale.order.line', 'search_read', [domain], {'fields': fields})
    
    # Obtener información de los pedidos para saber su estado de entrega
    order_domain = [('id', 'in', order_ids)]
    order_fields = ['id', 'delivery_status']
    orders_info = models.execute_kw(DB, uid, PWD,
        'sale.order', 'search_read', [order_domain], {'fields': order_fields})
    
    # Crear diccionario de estados de pedidos
    order_status = {order['id']: order['delivery_status'] for order in orders_info}
    
    # Filtrar líneas para pedidos parciales: solo mostrar donde falten productos por entregar
    filtered_lines = []
    for line in lines:
        order_id = line['order_id'][0]
        delivery_status = order_status.get(order_id, 'pending')
        
        if delivery_status == 'partial':
            # Para pedidos parciales, solo incluir líneas donde falten productos
            qty_pedida = line['product_uom_qty']
            qty_entregada = line['qty_delivered']
            qty_faltante = qty_pedida - qty_entregada
            
            if qty_faltante > 0:
                # Actualizar la cantidad requerida a la cantidad faltante
                line['product_uom_qty'] = qty_faltante
                filtered_lines.append(line)
        else:
            # Para pedidos pending y started, incluir todas las líneas
            filtered_lines.append(line)
    
    print(f"✅ Obtenidas {len(filtered_lines)} líneas de pedido (filtradas para pedidos parciales)")
    return filtered_lines


def get_recent_mrp(models, uid, product_ids, partner_id=None):
    """Versión optimizada que obtiene producción reciente para múltiples productos"""
    two_weeks_ago = (datetime.today() - timedelta(weeks=4)).strftime('%Y-%m-%d')
    domain = [
        ('state', '=', 'done'),
        ('product_id', 'in', product_ids),
        ('date_finished', '>=', two_weeks_ago)
    ]
    if partner_id is not None:
        domain.insert(1, ('partner_id', '=', partner_id))
    fields = ['product_id', 'product_qty', 'package_producing_id']
    return models.execute_kw(DB, uid, PWD, 'mrp.production', 'search_read', [domain], {'fields': fields})


def get_pending_mrp_all(models, uid):
    """Obtiene todas las órdenes de producción pendientes (MAQ y APERITIVO) en una sola consulta"""
    print("🔍 Obteniendo órdenes de fabricación en estados activos con origin contiene 'MAQ' y 'APERITIVO'...")
    domain = [
        ('state', 'in', ('draft', 'confirmed', 'progress', 'to_close')),
        ('origin', 'ilike', 'MAQ')
    ]
    fields = ['id', 'origin', 'product_id', 'product_qty', 'partner_id', 'state']
    orders_maq = models.execute_kw(DB, uid, PWD, 'mrp.production', 'search_read',
        [domain], {'fields': fields, 'order': 'origin'})
    
    domain_ap = [
        ('state', 'in', ('draft', 'confirmed', 'progress', 'to_close')),
        ('origin', 'ilike', 'APERITIVO')
    ]
    orders_ap = models.execute_kw(DB, uid, PWD, 'mrp.production', 'search_read',
        [domain_ap], {'fields': fields, 'order': 'origin'})
    
    all_orders = orders_maq + orders_ap
    print(f"✅ Encontradas {len(all_orders)} órdenes en estados activos MAQ y AP")
    return all_orders


def check_package_stock_batch(models, uid, package_ids):
    """Verifica stock de múltiples packs de una vez"""
    if not package_ids:
        return {}
    
    domain = [
        ('package_id', 'in', package_ids),
        ('location_id', 'ilike', 'WH/Stock/'),
         ('location_id', 'not ilike', 'Salida'),
        ('quantity', '>', 0)
    ]
    
    stock_quants = models.execute_kw(DB, uid, PWD,
        'stock.quant', 'search_read', [domain], {'fields': ['quantity', 'package_id']})
    
    # Agrupar por package_id
    stock_by_package = defaultdict(int)
    for quant in stock_quants:
        stock_by_package[quant['package_id'][0]] += quant['quantity']
    
    return stock_by_package


@app.route('/pedidos_nacional', methods=['GET'])
def pedidos_nacional():
    try:
        uid, models = connect()
        partners = get_no_autoplan_ruta_partners(models, uid)
        partner_ids = [p['id'] for p in partners]
        partner_dict = {p['id']: p['name'] for p in partners}
        orders = get_pending_orders(models, uid, partner_ids)
        order_ids = [o['id'] for o in orders]
        lines = get_order_lines(models, uid, order_ids)
        lines_by_order = defaultdict(list)
        for line in lines:
            lines_by_order[line['order_id'][0]].append(line)
        pending_production = get_pending_mrp_all(models, uid)
        maquina_tiempos = defaultdict(lambda: {'tiempo_total': 0, 'ordenes': []})
        now = datetime.now()
        for mo in pending_production:
            maquina = mo['origin']
            qty = mo['product_qty']
            mins = qty / 11
            maquina_tiempos[maquina]['tiempo_total'] += mins
            maquina_tiempos[maquina]['ordenes'].append({
                'product_id': mo['product_id'][0],
                'cantidad': qty,
                'tiempo': mins,
                'estado': mo['state'],
                'fecha_fin': (now + timedelta(minutes=maquina_tiempos[maquina]['tiempo_total'])).strftime('%Y-%m-%d %H:%M')
            })
        all_product_ids = list(set(line['product_id'][0] for line in lines))
        recent_production = get_recent_mrp(models, uid, all_product_ids)
        all_package_ids = list(set(prod['package_producing_id'][0] for prod in recent_production 
                                  if prod.get('package_producing_id')))
        package_stock_cache = check_package_stock_batch(models, uid, all_package_ids)
        packs_by_product = defaultdict(list)
        for prod in recent_production:
            if not prod.get('package_producing_id'):
                continue
            package_id = prod['package_producing_id'][0]
            cantidad = package_stock_cache.get(package_id, 0)
            if cantidad <= 0:
                continue
            partner_id = prod.get('partner_id') if 'partner_id' in prod else None
            packs_by_product[prod['product_id'][0]].append({
                'package_id': package_id,
                'cantidad': cantidad,
                'partner_id': partner_id,
            })
        orders_sorted = sorted(orders, key=lambda o: o.get('commitment_date', '9999-12-31'))
        summary = defaultdict(lambda: defaultdict(lambda: {
            'total_pedido': 0,
            'total_producido': 0,
            'total_faltante': 0,
            'productos_faltantes': [],
            'estado_stock': 'Pendiente',
            'commitment_date': None,
            'fecha_estimada': None,
            'estado_tiempo': "",
            'delivery_status': 'unknown',
            'packs_asignados': []
        }))
        lines_by_order = defaultdict(list)
        for line in lines:
            lines_by_order[line['order_id'][0]].append(line)
        for order in orders_sorted:
            order_id = order['id']
            client_id = order['partner_id'][0]
            client = partner_dict.get(client_id, 'Desconocido')
            for line in lines_by_order[order_id]:
                pid_prod = line['product_id'][0]
                name = line['name']
                req = line['product_uom_qty']
                falt = req
                packs = packs_by_product[pid_prod]
                for pack in packs:
                    if falt <= 0:
                        break
                    if pack['cantidad'] <= 0:
                        continue
                    if pack['partner_id'] == client_id:
                        usar = min(falt, pack['cantidad'])
                        pack['cantidad'] -= usar
                        falt -= usar
                        summary[client][order['name']]['packs_asignados'].append({'pack': pack['package_id'], 'cantidad': usar, 'cliente': client_id})
                for pack in packs:
                    if falt <= 0:
                        break
                    if pack['cantidad'] <= 0:
                        continue
                    if not pack['partner_id']:
                        usar = min(falt, pack['cantidad'])
                        pack['cantidad'] -= usar
                        falt -= usar
                        summary[client][order['name']]['packs_asignados'].append({'pack': pack['package_id'], 'cantidad': usar, 'cliente': None})
                total_ok = req - falt
                summary[client][order['name']]['total_pedido'] += req
                summary[client][order['name']]['total_producido'] += total_ok
                summary[client][order['name']]['total_faltante'] += falt
                summary[client][order['name']]['commitment_date'] = order.get('commitment_date', 'Sin fecha')
                summary[client][order['name']]['delivery_status'] = order.get('delivery_status', 'unknown')
                if falt > 0:
                    summary[client][order['name']]['productos_faltantes'].append({
                        'nombre': name,
                        'faltante': falt
                    })
        all_orders = []
        for client, orders in summary.items():
            for order_name, data in orders.items():
                is_completo = (data['total_faltante'] == 0 or data['estado_stock'] == 'Cubierto')
                estado_pedido = "✅ COMPLETO" if is_completo else "❌ INCOMPLETO"
                if data['total_pedido'] > 0:
                    porcentaje_completado = ((data['total_pedido'] - data['total_faltante']) / data['total_pedido']) * 100
                    porcentaje_faltante = 100 - porcentaje_completado
                else:
                    porcentaje_completado = 100
                    porcentaje_faltante = 0
                mismo_dia = False
                if not is_completo and data['fecha_estimada'] and data['commitment_date'] and data['commitment_date'] != 'Sin fecha':
                    try:
                        fecha_entrega = datetime.strptime(data['commitment_date'], '%Y-%m-%d %H:%M:%S')
                        fecha_estimada = datetime.strptime(data['fecha_estimada'], '%Y-%m-%d %H:%M')
                        mismo_dia = fecha_entrega.date() == fecha_estimada.date()
                    except:
                        mismo_dia = False
                order_info = {
                    'cliente': client,
                    'nombre': order_name,
                    'estado': estado_pedido,
                    'fecha_entrega': data['commitment_date'],
                    'total_pedido': data['total_pedido'],
                    'total_producido': data['total_producido'],
                    'total_faltante': data['total_faltante'],
                    'estado_stock': data['estado_stock'],
                    'productos_faltantes': data['productos_faltantes'],
                    'porcentaje_completado': round(porcentaje_completado, 2),
                    'porcentaje_faltante': round(porcentaje_faltante, 2),
                    'fecha_estimada': data['fecha_estimada'],
                    'estado_tiempo': data['estado_tiempo'],
                    'is_completo': is_completo,
                    'mismo_dia': mismo_dia,
                    'delivery_status': data['delivery_status'],
                    'is_started': data['delivery_status'] == 'started'
                }
                all_orders.append(order_info)
        all_orders.sort(key=lambda x: x['fecha_entrega'] if x['fecha_entrega'] != 'Sin fecha' else '9999-12-31')
        pedidos_completos = [o for o in all_orders if o['is_completo']]
        incompletos_mismo_dia = [o for o in all_orders if not o['is_completo'] and o['mismo_dia']]
        incompletos_otros = [o for o in all_orders if not o['is_completo'] and not o['mismo_dia']]
        incompletos_otros.sort(key=lambda x: x['porcentaje_completado'], reverse=True)
        return jsonify({
            "COMPLETOS": pedidos_completos,
            "INCOMPLETOS_MISMO_DIA": incompletos_mismo_dia,
            "INCOMPLETOS_OTROS": incompletos_otros,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def main():
    uid, models = connect()

    # Obtener SOLO partners SIN etiqueta AutoplanES
    partners = get_no_autoplan_ruta_partners(models, uid)
    partner_ids = [p['id'] for p in partners]
    partner_dict = {p['id']: p['name'] for p in partners}
    
    # Obtener pedidos y líneas en una sola llamada
    orders = get_pending_orders(models, uid, partner_ids)
    order_ids = [o['id'] for o in orders]
    lines = get_order_lines(models, uid, order_ids)
    
    # Crear diccionario de líneas por pedido
    lines_by_order = defaultdict(list)
    for line in lines:
        lines_by_order[line['order_id'][0]].append(line)
    
    # Obtener todas las órdenes de producción pendientes
    pending_production = get_pending_mrp_all(models, uid)
    
    # Calcular tiempos estimados por máquina
    maquina_tiempos = defaultdict(lambda: {'tiempo_total': 0, 'ordenes': []})
    now = datetime.now()
    
    for mo in pending_production:
        maquina = mo['origin']
        qty = mo['product_qty']
        mins = qty / 11
        maquina_tiempos[maquina]['tiempo_total'] += mins
        maquina_tiempos[maquina]['ordenes'].append({
            'product_id': mo['product_id'][0],
            'cantidad': qty,
            'tiempo': mins,
            'estado': mo['state'],
            'fecha_fin': (now + timedelta(minutes=maquina_tiempos[maquina]['tiempo_total'])).strftime('%Y-%m-%d %H:%M')
        })
    
    # Obtener todos los product_ids únicos
    all_product_ids = list(set(line['product_id'][0] for line in lines))

    # Obtener producción reciente para todos los productos de una vez
    recent_production = get_recent_mrp(models, uid, all_product_ids)
    # Obtener todos los package_ids únicos de la producción reciente
    all_package_ids = list(set(prod['package_producing_id'][0] for prod in recent_production 
                              if prod.get('package_producing_id')))
    # Verificar stock de todos los packs de una vez
    package_stock_cache = check_package_stock_batch(models, uid, all_package_ids)

    # --- NUEVA LÓGICA DE ASIGNACIÓN DE PACKS ---
    # 1. Construir estructura de packs disponibles por producto
    packs_by_product = defaultdict(list)
    for prod in recent_production:
        if not prod.get('package_producing_id'):
            continue
        package_id = prod['package_producing_id'][0]
        cantidad = package_stock_cache.get(package_id, 0)
        if cantidad <= 0:
            continue
        partner_id = prod.get('partner_id') if 'partner_id' in prod else None
        packs_by_product[prod['product_id'][0]].append({
            'package_id': package_id,
            'cantidad': cantidad,
            'partner_id': partner_id,
        })

    # 2. Ordenar pedidos por fecha de entrega (FIFO)
    orders_sorted = sorted(orders, key=lambda o: o.get('commitment_date', '9999-12-31'))

    # 3. Asignar packs a pedidos por antigüedad y prioridad de cliente
    summary = defaultdict(lambda: defaultdict(lambda: {
        'total_pedido': 0,
        'total_producido': 0,
        'total_faltante': 0,
        'productos_faltantes': [],
        'estado_stock': 'Pendiente',
        'commitment_date': None,
        'fecha_estimada': None,
        'estado_tiempo': "",
        'delivery_status': 'unknown',
        'packs_asignados': []
    }))
    lines_by_order = defaultdict(list)
    for line in lines:
        lines_by_order[line['order_id'][0]].append(line)

    for order in orders_sorted:
        order_id = order['id']
        client_id = order['partner_id'][0]
        client = partner_dict.get(client_id, 'Desconocido')
        print(f"\n📦 Cliente: {client}")
        print(f"  Pedido {order['name']} (Entrega: {order['commitment_date']})")
        for line in lines_by_order[order_id]:
            pid_prod = line['product_id'][0]
            name = line['name']
            req = line['product_uom_qty']
            falt = req
            packs = packs_by_product[pid_prod]
            # 1. Prioridad: packs del cliente (partner_id == client_id)
            for pack in packs:
                if falt <= 0:
                    break
                if pack['cantidad'] <= 0:
                    continue
                if pack['partner_id'] == client_id:
                    usar = min(falt, pack['cantidad'])
                    pack['cantidad'] -= usar
                    falt -= usar
                    summary[client][order['name']]['packs_asignados'].append({'pack': pack['package_id'], 'cantidad': usar, 'cliente': client_id})
            # 2. Si falta, usar stock general (partner_id is None)
            for pack in packs:
                if falt <= 0:
                    break
                if pack['cantidad'] <= 0:
                    continue
                if not pack['partner_id']:
                    usar = min(falt, pack['cantidad'])
                    pack['cantidad'] -= usar
                    falt -= usar
                    summary[client][order['name']]['packs_asignados'].append({'pack': pack['package_id'], 'cantidad': usar, 'cliente': None})
            # 3. Si sigue faltando, no hay stock suficiente
            total_ok = req - falt
            mark = '✅' if falt == 0 else '❌'
            print(f"    {mark} {name}: Ped {req}, ProdOK {total_ok}, Faltan {falt}")
            if summary[client][order['name']]['packs_asignados']:
                for asignado in summary[client][order['name']]['packs_asignados']:
                    print(f"      → Pack {asignado['pack']} ({'cliente' if asignado['cliente'] else 'stock general'}): {asignado['cantidad']}")
            # Actualizar resumen
            summary[client][order['name']]['total_pedido'] += req
            summary[client][order['name']]['total_producido'] += total_ok
            summary[client][order['name']]['total_faltante'] += falt
            summary[client][order['name']]['commitment_date'] = order.get('commitment_date', 'Sin fecha')
            summary[client][order['name']]['delivery_status'] = order.get('delivery_status', 'unknown')
            if falt > 0:
                summary[client][order['name']]['productos_faltantes'].append({
                    'nombre': name,
                    'faltante': falt
                })

    # Mostrar resumen final por cliente
    print("\n" + "="*50)
    print("📊 RESUMEN FINAL POR CLIENTE Y PEDIDO")
    print("="*50)
    
    # Preparar todos los pedidos de todos los clientes
    all_orders = []
    for client, orders in summary.items():
        for order_name, data in orders.items():
            is_completo = (data['total_faltante'] == 0 or data['estado_stock'] == 'Cubierto')
            estado_pedido = "✅ COMPLETO" if is_completo else "❌ INCOMPLETO"
            
            # Calcular porcentaje correcto: (Total pedido - Faltante) / Total pedido
            if data['total_pedido'] > 0:
                porcentaje_completado = ((data['total_pedido'] - data['total_faltante']) / data['total_pedido']) * 100
                porcentaje_faltante = 100 - porcentaje_completado
            else:
                porcentaje_completado = 100
                porcentaje_faltante = 0
            
            # Verificar si se completará el mismo día
            mismo_dia = False
            if not is_completo and data['fecha_estimada'] and data['commitment_date'] and data['commitment_date'] != 'Sin fecha':
                try:
                    fecha_entrega = datetime.strptime(data['commitment_date'], '%Y-%m-%d %H:%M:%S')
                    fecha_estimada = datetime.strptime(data['fecha_estimada'], '%Y-%m-%d %H:%M')
                    mismo_dia = fecha_entrega.date() == fecha_estimada.date()
                except:
                    mismo_dia = False
            
            order_info = {
                'cliente': client,
                'nombre': order_name,
                'estado': estado_pedido,
                'fecha_entrega': data['commitment_date'],
                'total_pedido': data['total_pedido'],
                'total_producido': data['total_producido'],
                'total_faltante': data['total_faltante'],
                'estado_stock': data['estado_stock'],
                'productos_faltantes': data['productos_faltantes'],
                'porcentaje_completado': porcentaje_completado,
                'porcentaje_faltante': porcentaje_faltante,
                'fecha_estimada': data['fecha_estimada'],
                'estado_tiempo': data['estado_tiempo'],
                'is_completo': is_completo,
                'mismo_dia': mismo_dia,
                'delivery_status': data['delivery_status']
            }
            all_orders.append(order_info)
    
    # 1. Ordenar por fecha de entrega
    all_orders.sort(key=lambda x: x['fecha_entrega'] if x['fecha_entrega'] != 'Sin fecha' else '9999-12-31')
    
    # Separar en categorías
    pedidos_completos = [o for o in all_orders if o['is_completo']]
    incompletos_mismo_dia = [o for o in all_orders if not o['is_completo'] and o['mismo_dia']]
    incompletos_otros = [o for o in all_orders if not o['is_completo'] and not o['mismo_dia']]
    
    # 3. Ordenar incompletos por porcentaje de mayor a menor
    incompletos_otros.sort(key=lambda x: x['porcentaje_completado'], reverse=True)
    
    # Mostrar pedidos completos
    if pedidos_completos:
        print("\n  📦 Pedidos Completos:")
        print("  " + "-"*30)
        for order in pedidos_completos:
            is_started = "Sí" if order['delivery_status'] == 'started' else "No"
            print(f"\n    Cliente: {order['cliente']}")
            print(f"    Pedido: {order['nombre']}")
            print(f"      Estado: {order['estado']}")
            print(f"      Fecha de entrega: {order['fecha_entrega']}")
            print(f"      Total Pedido: {order['total_pedido']}")
            print(f"      Total Producido: {order['total_producido']}")
            print(f"      Estado Stock: {order['estado_stock']}")
            print(f"      Estado Started: {is_started}")
    
    # Mostrar incompletos que se completarán el mismo día
    if incompletos_mismo_dia:
        print("\n  ⚡ Incompletos (Se completarán el mismo día):")
        print("  " + "-"*30)
        for order in incompletos_mismo_dia:
            is_started = "Sí" if order['delivery_status'] == 'started' else "No"
            print(f"\n    Cliente: {order['cliente']}")
            print(f"    Pedido: {order['nombre']}")
            print(f"      Estado: {order['estado']}")
            print(f"      Fecha de entrega: {order['fecha_entrega']}")
            print(f"      Total Pedido: {order['total_pedido']}")
            print(f"      Total Producido: {order['total_producido']}")
            print(f"      Total Faltante: {order['total_faltante']}")
            print(f"      Progreso: {order['porcentaje_completado']:.1f}% completado ({order['porcentaje_faltante']:.1f}% pendiente)")
            print(f"      Fecha estimada: {order['fecha_estimada']}")
            print(f"      Estado: {order['estado_tiempo']}")
            print(f"      Estado Started: {is_started}")
            
            if order['productos_faltantes']:
                print("      Productos Faltantes:")
                for prod in order['productos_faltantes']:
                    fecha_est = prod.get('fecha_estimada', 'No estimada')
                    estado_tiempo = prod.get('estado_tiempo', '')
                    print(f"        - {prod['nombre']}: Faltan {prod['faltante']} (Disponible: {fecha_est}) {estado_tiempo}")
    
    # Mostrar incompletos ordenados por porcentaje
    if incompletos_otros:
        print("\n  ⚠️ Incompletos (Ordenados por % completado):")
        print("  " + "-"*30)
        for order in incompletos_otros:
            is_started = "Sí" if order['delivery_status'] == 'started' else "No"
            print(f"\n    Cliente: {order['cliente']}")
            print(f"    Pedido: {order['nombre']}")
            print(f"      Estado: {order['estado']}")
            print(f"      Fecha de entrega: {order['fecha_entrega']}")
            print(f"      Total Pedido: {order['total_pedido']}")
            print(f"      Total Producido: {order['total_producido']}")
            print(f"      Total Faltante: {order['total_faltante']}")
            print(f"      Progreso: {order['porcentaje_completado']:.1f}% completado ({order['porcentaje_faltante']:.1f}% pendiente)")
            print(f"      Estado Started: {is_started}")
            
            if order['fecha_estimada']:
                print(f"      Fecha estimada: {order['fecha_estimada']}")
                print(f"      Estado: {order['estado_tiempo']}")
            
            if order['productos_faltantes']:
                print("      Productos Faltantes:")
                for prod in order['productos_faltantes']:
                    fecha_est = prod.get('fecha_estimada', 'No estimada')
                    estado_tiempo = prod.get('estado_tiempo', '')
                    print(f"        - {prod['nombre']}: Faltan {prod['faltante']} (Disponible: {fecha_est}) {estado_tiempo}")
    
    print("\n" + "="*50)

    print("\n✅ Script completado")

if __name__ == '__main__':
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

