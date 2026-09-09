"""
tests/panel/test_contacts_pending_cut.py
========================================

Punto 1: un contacto cuyo ultimo mensaje es del CLIENTE entra siempre en la
lista del panel, tenga badge de no-leido o no.

El defecto que arregla, medido en produccion el 11-ago-2026: el panel pide
`filter_time=all` sin `limit` ni `page` (index.js:1381), asi que recibe los no
leidos + los 30 mas recientes. Abrir un chat quita el no-leido pero no la deuda
con el cliente, asi que el contacto caia al monton ordenado por fecha y, pasada
la posicion 30, desaparecia de la barra. La UI no pagina: no habia forma de
volver a el. 13 de 40 pendientes estaban en ese agujero.

Cada test esta escrito para fallar contra el codigo previo al arreglo.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

os.environ.setdefault("HUBSPOT_API_KEY", "test-dummy-key")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")
os.environ.setdefault("ADMIN_API_KEY", "test-admin")

import middleware.outbound_panel as panel  # noqa: E402
from middleware.outbound_panel import (  # noqa: E402
    CONTACTS_INBOX_FALLBACK_MULTIPLIER,
    CONTACTS_PENDING_MAX,
    _cuenta_como_prioridad,
    _nunca_se_corta,
    _resolve_pending_reply_phones,
    _split_always_visible,
)

LIMIT = 30


def _contacts(n, prefix="+5730000"):
    """n contactos en orden de actividad reciente, como los entrega el ZSET."""
    return [{"phone": f"{prefix}{i:04d}", "canal": "whatsapp"} for i in range(n)]


def _phones(contacts):
    return [c["phone"] for c in contacts]


# ── El rescate: el pendiente entra aunque este sepultado ──────────────────────


def test_pendiente_lejos_del_corte_entra_igual():
    """
    El caso real: un contacto en la posicion 150, sin badge, con el cliente
    esperando desde hace dias.

    Mutacion: si se ignora `pending_phones`, el contacto no aparece y el test
    rompe — que es exactamente el comportamiento anterior al arreglo.
    """
    contacts = _contacts(200)
    sepultado = contacts[150]["phone"]

    activos, stats = _split_always_visible(
        contacts, unread_phones=set(), pending_phones={sepultado},
        page=1, limit=LIMIT,
    )

    assert sepultado in _phones(activos), (
        "El contacto con el cliente esperando sigue fuera de la lista"
    )
    assert stats["pending"] == 1
    assert len(activos) == LIMIT + 1


def test_sin_pendientes_el_corte_es_el_de_siempre():
    """Sin pendientes, el comportamiento debe ser identico al anterior."""
    contacts = _contacts(200)
    unread = {contacts[0]["phone"], contacts[99]["phone"]}

    activos, stats = _split_always_visible(
        contacts, unread_phones=unread, pending_phones=set(), page=1, limit=LIMIT,
    )

    assert stats["unread"] == 2
    assert stats["pending"] == 0
    assert len(activos) == 2 + LIMIT


def test_pendiente_que_ademas_es_no_leido_no_se_duplica():
    """
    Mutacion: si las dos reglas se aplicaran por separado y se concatenaran,
    el contacto saldria dos veces y el panel mostraria una fila fantasma.
    """
    contacts = _contacts(50)
    ambos = contacts[40]["phone"]

    activos, stats = _split_always_visible(
        contacts, unread_phones={ambos}, pending_phones={ambos},
        page=1, limit=LIMIT,
    )

    assert _phones(activos).count(ambos) == 1
    assert stats["unread"] == 1
    assert stats["pending"] == 0, "Contado como no-leido, no dos veces"


def test_pendiente_reciente_no_sale_dos_veces():
    """
    Un pendiente que ademas cae dentro del corte normal esta en las dos listas
    candidatas. Si se anadiera a ambas, el panel pintaria la fila duplicada.

    Mutacion: anadir el pendiente tambien a `rest` deja en verde todo lo demas
    —el contacto sigue apareciendo— y solo este test lo caza.
    """
    contacts = _contacts(200)
    reciente = contacts[5]["phone"]

    activos, _ = _split_always_visible(
        contacts, unread_phones=set(), pending_phones={reciente},
        page=1, limit=LIMIT,
    )

    telefonos = _phones(activos)
    assert telefonos.count(reciente) == 1, "El pendiente sale duplicado en la lista"
    assert len(telefonos) == len(set(telefonos)), "Hay filas duplicadas en la respuesta"


def test_ningun_contacto_se_repite_con_las_dos_reglas_activas():
    """Barrido: no-leidos y pendientes solapados, todos dentro del corte."""
    contacts = _contacts(40)
    unread = set(_phones(contacts[0:10]))
    pending = set(_phones(contacts[5:20]))

    activos, _ = _split_always_visible(
        contacts, unread_phones=unread, pending_phones=pending,
        page=1, limit=LIMIT,
    )

    telefonos = _phones(activos)
    assert len(telefonos) == len(set(telefonos)), (
        f"{len(telefonos) - len(set(telefonos))} contactos duplicados"
    )


def test_se_conserva_el_orden_de_actividad():
    """Los 'siempre visibles' van primero, en el orden en que venian."""
    contacts = _contacts(100)
    pendientes = {contacts[80]["phone"], contacts[10]["phone"]}

    activos, _ = _split_always_visible(
        contacts, unread_phones=set(), pending_phones=pendientes,
        page=1, limit=LIMIT,
    )

    assert _phones(activos)[:2] == [contacts[10]["phone"], contacts[80]["phone"]]


# ── El tope: la proteccion contra el coste de HubSpot ─────────────────────────


def test_el_tope_de_pendientes_acota_la_lista():
    """
    Cada contacto extra cuesta enriquecimiento de HubSpot. Si
    `last_message_sender` viniera mal para cientos, sin tope volveria el coste
    de los 508s del 10-ago.

    Mutacion: quitar el tope hace que entren los 250 y este test rompe.
    """
    contacts = _contacts(300)
    todos_pendientes = set(_phones(contacts[:250]))

    activos, stats = _split_always_visible(
        contacts, unread_phones=set(), pending_phones=todos_pendientes,
        page=1, limit=LIMIT,
    )

    assert stats["pending"] == CONTACTS_PENDING_MAX
    assert stats["pending_truncated"] == 250 - CONTACTS_PENDING_MAX
    assert len(activos) == CONTACTS_PENDING_MAX + LIMIT


def test_los_pendientes_que_pasan_del_tope_no_se_pierden():
    """Caen al resto y compiten por el corte normal, no desaparecen del universo."""
    contacts = _contacts(300)
    todos_pendientes = set(_phones(contacts[:250]))

    activos, _ = _split_always_visible(
        contacts, unread_phones=set(), pending_phones=todos_pendientes,
        page=1, limit=LIMIT, pending_max=10,
    )

    salieron = set(_phones(activos))
    # Los 10 primeros por el pase de pendiente, y los siguientes por el corte normal.
    assert contacts[0]["phone"] in salieron
    assert contacts[10]["phone"] in salieron, (
        "El pendiente que paso del tope quedo fuera hasta del corte normal"
    )


def test_el_tope_se_queda_con_los_mas_recientes_no_con_los_primeros():
    """
    `advisor_contacts` llega en dos tramos: el ZSET y despues el respaldo de
    MongoDB. Aplicando el tope por posicion, un pendiente reciente que venga del
    segundo tramo pierde su plaza frente a otro mucho mas antiguo del primero.
    Medido en produccion el 12-ago-2026: 3 de los mas recientes quedaban fuera.

    Mutacion: volver a `if pending_count < pending_max` deja este test en rojo.
    """
    antiguos = [
        {"phone": f"+5730000{i:04d}", "last_activity": "2026-06-01T10:00:00-05:00"}
        for i in range(5)
    ]
    reciente = {"phone": "+573009999999", "last_activity": "2026-08-12T01:00:00-05:00"}
    contacts = antiguos + [reciente]  # el reciente llega el ULTIMO, como el respaldo

    activos, stats = _split_always_visible(
        contacts, unread_phones=set(),
        pending_phones=set(_phones(contacts)),
        page=1, limit=LIMIT, pending_max=3,
    )

    rescatados = [c["phone"] for c in activos if c.get("pending_rescued")]
    assert reciente["phone"] in rescatados, (
        "El pendiente mas reciente pierde su plaza por llegar el ultimo en la lista"
    )
    assert stats["pending"] == 3
    assert stats["pending_truncated"] == 3


def test_el_no_leido_no_gasta_plaza_de_pendiente():
    """Un contacto sin leer ya entra por su propia via; no debe competir."""
    contacts = _contacts(10)
    todos = set(_phones(contacts))

    activos, stats = _split_always_visible(
        contacts, unread_phones={contacts[0]["phone"]}, pending_phones=todos,
        page=1, limit=LIMIT, pending_max=2,
    )

    assert stats["unread"] == 1
    assert stats["pending"] == 2, "El no-leido consumio una de las plazas del tope"
    assert len([c for c in activos if c.get("pending_rescued")]) == 2


def test_el_tope_por_defecto_deja_margen_sobre_lo_medido():
    """13 pendientes medidos el 11-ago; el tope no puede quedar por debajo."""
    assert CONTACTS_PENDING_MAX >= 50, (
        f"Tope de {CONTACTS_PENDING_MAX}: demasiado cerca del volumen real medido"
    )


# ── Interaccion con la degradacion del inbox ──────────────────────────────────


def test_con_el_inbox_caido_los_pendientes_entran_igual():
    """
    Las dos protecciones son independientes: que el inbox no responda no puede
    anular el rescate de los pendientes.

    Mutacion: si la rama degradada volviera a cortar sobre la lista completa
    ignorando `pending_phones`, el sepultado desaparece y esto rompe.
    """
    contacts = _contacts(200)
    sepultado = contacts[150]["phone"]

    activos, stats = _split_always_visible(
        contacts, unread_phones=None, pending_phones={sepultado},
        page=1, limit=LIMIT,
    )

    assert stats["inbox_down"] is True
    assert sepultado in _phones(activos)
    assert len(activos) == 1 + LIMIT * CONTACTS_INBOX_FALLBACK_MULTIPLIER


def test_la_paginacion_no_pierde_ni_repite_del_resto():
    contacts = _contacts(200)

    p1, _ = _split_always_visible(contacts, set(), set(), page=1, limit=LIMIT)
    p2, _ = _split_always_visible(contacts, set(), set(), page=2, limit=LIMIT)

    assert not (set(_phones(p1)) & set(_phones(p2)))
    assert _phones(p2)[0] == contacts[LIMIT]["phone"]


def test_los_siempre_visibles_se_repiten_en_cada_pagina():
    """
    Comportamiento conservado, no nuevo: hoy los no-leidos ya salen en todas las
    paginas. Los pendientes siguen la misma regla para no cambiar el contrato.
    """
    contacts = _contacts(200)
    pendiente = contacts[150]["phone"]

    p1, _ = _split_always_visible(contacts, set(), {pendiente}, page=1, limit=LIMIT)
    p2, _ = _split_always_visible(contacts, set(), {pendiente}, page=2, limit=LIMIT)

    assert pendiente in _phones(p1)
    assert pendiente in _phones(p2)


def test_lista_vacia_no_revienta():
    activos, stats = _split_always_visible([], set(), set(), page=1, limit=LIMIT)
    assert activos == []
    assert stats["total"] == 0


def test_contacto_sin_telefono_no_entra_por_error():
    """Un phone vacio no puede colarse en el conjunto de siempre-visibles."""
    contacts = [{"phone": "", "canal": "whatsapp"}] + _contacts(10)

    activos, stats = _split_always_visible(contacts, {""}, {""}, page=1, limit=LIMIT)

    assert stats["unread"] == 0
    assert stats["pending"] == 0
    assert len(activos) == 11  # todos caben en el corte, pero como "resto"


# ── La fuente del dato: MongoDB conversations ─────────────────────────────────


class _MongoFalso:
    """
    Doble de `conversations`, que guarda UN DOCUMENTO POR (telefono, canal).

    DELEGA en phones_awaiting_from_docs, la regla real, en vez de reimplementarla.
    Un doble que reimplementa la semantica no prueba el codigo: prueba lo que su
    autor cree que el codigo hace, y sobrevive a las mutaciones. Ya paso con la
    version anterior de este doble, que codificaba "basta con que algun canal
    tenga client" — la regla equivocada que dejo 20 badges encendidos.
    """

    def __init__(self, docs=None, revienta=False):
        self._docs = list(docs or [])
        self._revienta = revienta
        self.consultas = []

    async def find_phones_awaiting_reply(self, phones):
        self.consultas.append(list(phones))
        if self._revienta:
            raise RuntimeError("MongoServerSelectionTimeoutError")

        from database.mongodb_client import phones_awaiting_from_docs

        pedidos = set(phones)
        return phones_awaiting_from_docs(
            [d for d in self._docs if d.get("phone") in pedidos]
        )


def _con_mongo(monkeypatch, fake):
    monkeypatch.setattr(panel, "get_mongo_manager", lambda: fake)


def test_solo_cuenta_como_pendiente_lo_que_dejo_el_cliente(monkeypatch):
    """
    Mutacion: si se aceptara cualquier `last_message_sender`, entrarian tambien
    las conversaciones que la asesora ya contesto y la lista se llenaria de ruido.
    """
    fake = _MongoFalso([
        _doc("+573000000001", "whatsapp", "client", 5),
        _doc("+573000000002", "whatsapp", "advisor", 5),
        _doc("+573000000003", "whatsapp", "bot", 5),
        _doc("+573000000004", "whatsapp", "", 5),
    ])
    _con_mongo(monkeypatch, fake)

    got = asyncio.run(_resolve_pending_reply_phones([
        {"phone": f"+57300000000{i}"} for i in range(1, 5)
    ]))

    assert got == {"+573000000001"}


def test_el_canal_contestado_no_tapa_al_que_sigue_esperando(monkeypatch):
    """
    El cliente escribio por el portal DESPUES de que se contestara por WhatsApp.
    Ese contacto espera respuesta y tiene que aparecer.

    Mutacion: colapsar los documentos por telefono hace que el resultado dependa
    del orden del cursor. Costo real: 3 clientes invisibles llevando 11 horas,
    4 dias y 6 dias.
    """
    fake = _MongoFalso([
        _doc("+573001110001", "whatsapp", "advisor", 3),
        _doc("+573001110001", "pagina_web", "client", 7),
    ])
    _con_mongo(monkeypatch, fake)

    got = asyncio.run(_resolve_pending_reply_phones([{"phone": "+573001110001"}]))

    assert got == {"+573001110001"}, (
        "El canal contestado tapa al canal donde el cliente sigue esperando"
    )


def test_el_canal_viejo_no_mantiene_el_aviso_encendido(monkeypatch):
    """
    El inverso, que es el que se me escapo: la asesora contesto por WhatsApp
    DESPUES de que el cliente escribiera por el portal. Ya no espera nadie.

    Mutacion: marcar el telefono si ALGUN canal tiene `client` deja el aviso
    encendido para siempre. Costo real: 20 badges falsos de 160, detectados el
    12-ago-2026 a las 11:05.
    """
    fake = _MongoFalso([
        _doc("+573001110002", "finca_raiz", "client", 11),
        _doc("+573001110002", "whatsapp", "advisor", 12),
    ])
    _con_mongo(monkeypatch, fake)

    got = asyncio.run(_resolve_pending_reply_phones([{"phone": "+573001110002"}]))

    assert got == set(), "El aviso sigue encendido despues de que se contestara"


# ── La consulta REAL, no un doble que la reimplemente ─────────────────────────
#
# Los tests de arriba usan un doble que aplica la semantica esperada, asi que
# comprueban el cableado pero no la consulta. Estos ejercitan
# find_phones_awaiting_reply() de verdad, con una coleccion falsa que SI filtra
# segun el query que reciba.


class _CursorAsincrono:
    def __init__(self, docs):
        self._docs = docs

    def __aiter__(self):
        async def gen():
            for d in self._docs:
                yield d
        return gen()


class _ConversationsQueFiltran:
    """Aplica el filtro que reciba, para que la consulta real se pruebe sola."""

    def __init__(self, docs):
        self.docs = docs

    def find(self, query, projection=None):
        salida = []
        for d in self.docs:
            ok = True
            for campo, cond in query.items():
                valor = d.get(campo)
                if isinstance(cond, dict) and "$in" in cond:
                    if valor not in cond["$in"]:
                        ok = False
                        break
                elif valor != cond:
                    ok = False
                    break
            if ok:
                # Respeta la proyeccion que reciba, como haria MongoDB. Devolver
                # siempre todos los campos ocultaria que la consulta se dejara de
                # pedir el remitente o la fecha, que es justo lo que la regla
                # necesita para decidir.
                campos = [k for k, v in (projection or {}).items() if v == 1]
                salida.append({k: d.get(k) for k in campos} if campos else dict(d))
        return _CursorAsincrono(salida)


def _mongo_con_documentos(docs):
    from database.mongodb_client import MongoDBManager

    mgr = MongoDBManager.__new__(MongoDBManager)

    class _Db:
        pass

    db = _Db()
    db.conversations = _ConversationsQueFiltran(docs)
    mgr.db = db

    async def _connect():
        return True

    mgr.connect = _connect
    return mgr


def _doc(phone, canal, sender, dia):
    from datetime import datetime

    return {"phone": phone, "canal": canal, "last_message_sender": sender,
            "last_message_at": datetime(2026, 8, dia, 12, 0)}


def test_decide_el_documento_mas_reciente_no_cualquiera():
    """
    El caso que rompio el badge el 12-ago-2026 a las 11:05.

    El cliente escribe por el portal y la asesora contesta por WhatsApp un minuto
    despues — el portal es solo el origen, el canal de mensajeria es WhatsApp. Si
    basta con que ALGUN canal tenga `client`, el aviso se queda encendido para
    siempre. Manda el mas reciente.

    Mutacion: volver a "algun documento con client" deja este test en rojo.
    """
    from database.mongodb_client import phones_awaiting_from_docs

    got = phones_awaiting_from_docs([
        _doc("+573001110002", "finca_raiz", "client", 11),
        _doc("+573001110002", "whatsapp", "advisor", 12),
    ])

    assert got == set(), "El aviso sigue encendido despues de que la asesora contestara"


def test_el_cliente_que_escribio_ultimo_si_espera():
    """
    El simetrico: la asesora contesto por WhatsApp hace dias y el cliente ha
    vuelto a escribir por el portal. Ese SI espera.

    Mutacion: quedarse con un documento cualquiera (colapsar) hace que dependa
    del orden del cursor y este test se vuelve inestable o rojo.
    """
    from database.mongodb_client import phones_awaiting_from_docs

    got = phones_awaiting_from_docs([
        _doc("+573001110001", "whatsapp", "advisor", 3),
        _doc("+573001110001", "pagina_web", "client", 7),
    ])

    assert got == {"+573001110001"}


def test_el_orden_de_los_documentos_no_cambia_el_resultado():
    """La regla no puede depender de en que orden los devuelva MongoDB."""
    from database.mongodb_client import phones_awaiting_from_docs

    a = _doc("+573001112233", "finca_raiz", "client", 5)
    b = _doc("+573001112233", "whatsapp", "advisor", 9)

    assert phones_awaiting_from_docs([a, b]) == phones_awaiting_from_docs([b, a])
    assert phones_awaiting_from_docs([a, b]) == set()


def test_un_documento_sin_fecha_no_decide():
    """
    Sin fecha se trata como lo mas antiguo; no puede pisar a uno fechado.

    Se prueban los DOS ordenes a proposito: con el sin-fecha primero, tratarlo
    como el mas nuevo daria el mismo resultado por casualidad, y la regla
    quedaria sin comprobar.
    """
    from database.mongodb_client import phones_awaiting_from_docs

    sin_fecha = {"phone": "+573001112233", "canal": "instagram",
                 "last_message_sender": "client", "last_message_at": None}
    fechado = _doc("+573001112233", "whatsapp", "advisor", 9)

    assert phones_awaiting_from_docs([sin_fecha, fechado]) == set()
    assert phones_awaiting_from_docs([fechado, sin_fecha]) == set(), (
        "Un documento sin fecha pisa al fechado y enciende el aviso"
    )


def test_un_solo_canal_se_comporta_como_siempre():
    from database.mongodb_client import phones_awaiting_from_docs

    assert phones_awaiting_from_docs([_doc("+57300", "whatsapp", "client", 5)]) == {"+57300"}
    assert phones_awaiting_from_docs([_doc("+57300", "whatsapp", "advisor", 5)]) == set()
    assert phones_awaiting_from_docs([_doc("+57300", "whatsapp", "bot", 5)]) == set()


def test_el_documento_sin_telefono_se_ignora():
    from database.mongodb_client import phones_awaiting_from_docs

    got = phones_awaiting_from_docs([
        {"phone": "", "last_message_sender": "client", "last_message_at": None},
        {"last_message_sender": "client", "last_message_at": None},
    ])

    assert got == set()


def test_la_consulta_real_no_colapsa_los_canales():
    """
    El caso exacto de produccion: el cliente espera en pagina_web y en whatsapp
    ya se contesto. Un solo documento por canal, el telefono debe salir.

    Mutacion: hacer que la consulta indexe por telefono (colapsando) deja esto
    en rojo — es el bug que dejo a 3 clientes esperando 11 horas, 4 y 6 dias.
    """
    mgr = _mongo_con_documentos([
        _doc("+573001110001", "pagina_web", "client", 7),
        _doc("+573001110001", "whatsapp", "advisor", 3),
    ])

    got = asyncio.run(mgr.find_phones_awaiting_reply(["+573001110001"]))

    assert got == {"+573001110001"}


def test_la_consulta_real_filtra_por_remitente():
    """
    Mutacion: quitar `last_message_sender: "client"` del filtro deja entrar a
    todas las conversaciones del asesor y este test lo caza.
    """
    mgr = _mongo_con_documentos([
        _doc("+573001112233", "whatsapp", "advisor", 5),
        _doc("+573004445566", "whatsapp", "client", 5),
    ])

    got = asyncio.run(mgr.find_phones_awaiting_reply(
        ["+573001112233", "+573004445566"]
    ))

    assert got == {"+573004445566"}


def test_la_consulta_real_solo_mira_los_telefonos_pedidos():
    mgr = _mongo_con_documentos([
        _doc("+573001112233", "whatsapp", "client", 5),
        _doc("+573009998877", "whatsapp", "client", 5),
    ])

    got = asyncio.run(mgr.find_phones_awaiting_reply(["+573001112233"]))

    assert got == {"+573001112233"}


def test_la_consulta_real_no_consulta_sin_telefonos():
    mgr = _mongo_con_documentos([
        {"phone": "+573001112233", "canal": "whatsapp", "last_message_sender": "client"},
    ])

    assert asyncio.run(mgr.find_phones_awaiting_reply([])) == set()


def test_si_ningun_canal_espera_el_telefono_no_es_pendiente(monkeypatch):
    """El caso simetrico: varios canales, ninguno con el cliente esperando."""
    fake = _MongoFalso([
        _doc("+573001112233", "whatsapp", "advisor", 5),
        _doc("+573001112233", "finca_raiz", "bot", 6),
    ])
    _con_mongo(monkeypatch, fake)

    got = asyncio.run(_resolve_pending_reply_phones([{"phone": "+573001112233"}]))

    assert got == set()


def test_si_mongo_falla_el_panel_se_comporta_como_antes(monkeypatch):
    """
    Fail-safe: sin el dato no se rescata a nadie, pero la lista no se rompe.

    Mutacion: si la excepcion se propagara, GET /contacts devolveria 500 y las
    asesoras se quedarian sin panel — mucho peor que el defecto que arregla.
    """
    _con_mongo(monkeypatch, _MongoFalso(revienta=True))

    got = asyncio.run(_resolve_pending_reply_phones([{"phone": "+573000000001"}]))

    assert got == set()


def test_no_consulta_si_no_hay_telefonos(monkeypatch):
    fake = _MongoFalso()
    _con_mongo(monkeypatch, fake)

    got = asyncio.run(_resolve_pending_reply_phones([{"canal": "whatsapp"}]))

    assert got == set()
    assert fake.consultas == [], "Consulto MongoDB sin ningun telefono que buscar"


def test_deduplica_telefonos_antes_de_consultar(monkeypatch):
    """No hace falta pedir dos veces el mismo telefono aunque venga en dos canales."""
    fake = _MongoFalso([_doc("+573000000001", "whatsapp", "client", 5)])
    _con_mongo(monkeypatch, fake)

    asyncio.run(_resolve_pending_reply_phones([
        {"phone": "+573000000001", "canal": "whatsapp"},
        {"phone": "+573000000001", "canal": "finca_raiz"},
    ]))

    assert fake.consultas == [["+573000000001"]]


def test_respuesta_none_de_mongo_no_revienta(monkeypatch):
    _con_mongo(monkeypatch, _MongoFalso(docs=None))

    got = asyncio.run(_resolve_pending_reply_phones([{"phone": "+573000000001"}]))

    assert got == set()


# ── El segundo corte, antes del enriquecimiento con HubSpot ───────────────────
#
# Lo que fallo en el primer deploy: _split_always_visible seleccionaba 110
# contactos y el pre-limite de HubSpot los dejaba en 62, porque solo respetaba
# `in_priority_zset`. La regla funcionaba y el resultado no cambiaba.


def test_marca_los_rescatados_para_el_segundo_corte():
    """
    Mutacion: sin la marca `pending_rescued`, el pre-limite de HubSpot vuelve a
    tirar los rescatados y el arreglo no llega a la pantalla.
    """
    contacts = _contacts(200)
    sepultado = contacts[150]

    _split_always_visible(
        contacts, unread_phones=set(), pending_phones={sepultado["phone"]},
        page=1, limit=LIMIT,
    )

    assert sepultado.get("pending_rescued") is True
    assert "pending_rescued" not in contacts[151], "Marco contactos que no esperan respuesta"


# ── La senal publica que pinta el panel (punto 1.2) ───────────────────────────


def test_pending_reply_es_veraz_aunque_ademas_tenga_sin_leer():
    """
    Un contacto puede esperar respuesta Y tener mensajes sin leer. Antes se
    marcaba solo a los rescatados, y la rama de no-leidos hacia `continue` antes
    de llegar a la marca — el panel no podia saber que ese contacto debia
    respuesta.

    Mutacion: mover la marca dentro de la rama de rescate deja esto en rojo.
    """
    contacts = _contacts(50)
    ambos = contacts[10]

    _split_always_visible(
        contacts, unread_phones={ambos["phone"]}, pending_phones={ambos["phone"]},
        page=1, limit=LIMIT,
    )

    assert ambos.get("pending_reply") is True, (
        "Un contacto sin leer que ademas espera respuesta no queda marcado"
    )


def test_pending_reply_es_veraz_por_encima_del_tope():
    """
    El tope gobierna a cuantos se RESCATA, no quien espera respuesta. Si uno de
    los que sobran acaba en la lista por el corte normal, tiene que pintar igual.
    """
    contacts = _contacts(300)
    todos = set(_phones(contacts[:250]))

    _split_always_visible(
        contacts, unread_phones=set(), pending_phones=todos,
        page=1, limit=LIMIT, pending_max=10,
    )

    por_encima = contacts[200]
    assert por_encima.get("pending_reply") is True, (
        "Un pendiente por encima del tope queda sin marcar y no pintaria aviso"
    )
    assert por_encima.get("pending_rescued") is None, (
        "Por encima del tope no puede llevar el pase de rescate"
    )


def test_quien_no_espera_respuesta_no_lleva_ninguna_marca():
    contacts = _contacts(20)

    _split_always_visible(contacts, unread_phones=set(), pending_phones=set(),
                          page=1, limit=LIMIT)

    for c in contacts:
        assert "pending_reply" not in c
        assert "pending_rescued" not in c


def test_el_prelimite_cuenta_al_rescatado_como_prioridad():
    """
    El predicado del segundo corte. Se prueba ejecutandolo, no buscandolo en el
    fuente: la primera version de este test miraba el texto y pasaba en falso
    porque la palabra aparecia en un comentario cercano.

    Mutacion: quitar `or contact.get("pending_rescued")` deja este test en rojo —
    es exactamente el bug que llego a produccion el 11-ago-2026.
    """
    assert _cuenta_como_prioridad({"in_priority_zset": False, "pending_rescued": True}) is True
    assert _cuenta_como_prioridad({"in_priority_zset": False}) is False
    assert _cuenta_como_prioridad({"in_priority_zset": True}) is True
    assert _cuenta_como_prioridad({}) is True, "Sin el campo se asume prioridad, como antes"


def test_la_verdad_sola_no_da_el_pase_en_los_cortes():
    """
    `pending_reply` es la verdad (el cliente espera) y `pending_rescued` es el
    pase (ademas cabe en el tope). Los cortes tienen que mirar el pase.

    Mutacion: si los predicados vuelven a mirar `pending_reply`, el tope de 100
    deja de existir —entran todos los pendientes historicos— y la latencia se
    dispara. Este test es lo unico que separa las dos marcas.
    """
    solo_verdad = {"in_priority_zset": False, "pending_reply": True}

    assert _cuenta_como_prioridad(solo_verdad) is False, (
        "Un pendiente por encima del tope pasa el pre-limite: el tope no sirve"
    )
    assert _nunca_se_corta(solo_verdad) is False, (
        "Un pendiente por encima del tope pasa el corte final: el tope no sirve"
    )


def test_el_rescatado_sobrevive_al_prelimite_completo():
    """
    Reproduce el corte entero: priority + bot[:remaining]. Un rescatado fuera del
    ZSET de prioridad debe seguir en la lista final.
    """
    seleccionados = [
        {"phone": f"+5730000{i:04d}", "in_priority_zset": False}
        for i in range(40)
    ]
    seleccionados[35]["pending_reply"] = True

    priority = [c for c in seleccionados if _cuenta_como_prioridad(c)]
    bot = [c for c in seleccionados if not _cuenta_como_prioridad(c)]
    sin_unread = sum(1 for c in priority if not c.get("has_unread", False))
    remaining = max(0, (LIMIT * 2) - sin_unread)
    final = priority + bot[:remaining]

    assert seleccionados[35] in final, (
        "El contacto con el cliente esperando no sobrevive al pre-limite de HubSpot"
    )


def test_el_endpoint_usa_el_predicado_en_el_prelimite():
    """Que el predicado sea correcto no sirve si el corte no lo llama."""
    import ast

    with open(panel.__file__, encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
        and n.name == "get_active_contacts"
    )
    seg = ast.get_source_segment(src, fn)

    assert "priority_contacts = [" in seg, "Desaparecio el pre-limite de HubSpot"
    assert "_cuenta_como_prioridad" in seg, (
        "El pre-limite no usa el predicado: los rescatados se seleccionan y "
        "luego se tiran, que es el bug del 11-ago-2026"
    )


# ── La cadena entera: los TRES cortes ─────────────────────────────────────────
#
# La leccion de este arreglo. GET /contacts recorta la lista tres veces:
#   1. seleccion            _split_always_visible
#   2. pre-limite HubSpot   priority_contacts + bot_contacts[:remaining]
#   3. respuesta final      _always_in_final + _rest_in_final[:limit]
# Arreglar uno solo no cambia nada en pantalla. Se comprobo dos veces en
# produccion el 11-ago-2026: primero quedaban 62 de 110, luego 33 de 136.


def _cadena_completa(contacts, unread, pending, limit=LIMIT):
    """Reproduce los tres cortes en el mismo orden que el endpoint."""
    seleccion, _ = _split_always_visible(contacts, unread, pending, page=1, limit=limit)

    priority = [c for c in seleccion if _cuenta_como_prioridad(c)]
    bot = [c for c in seleccion if not _cuenta_como_prioridad(c)]
    sin_unread = sum(1 for c in priority if not c.get("has_unread", False))
    tras_prelimite = priority + bot[:max(0, (limit * 2) - sin_unread)]

    always = [c for c in tras_prelimite if _nunca_se_corta(c)]
    rest = [c for c in tras_prelimite if not _nunca_se_corta(c)]
    return always + rest[:limit]


def test_el_pendiente_sobrevive_los_tres_cortes():
    """
    Mutacion: romper CUALQUIERA de los tres cortes deja este test en rojo. Es el
    unico que cubre lo que de verdad importa — que el contacto llegue a la
    respuesta, no que pase el primer filtro.
    """
    contacts = [
        {"phone": f"+5730000{i:04d}", "in_priority_zset": False}
        for i in range(200)
    ]
    sepultado = contacts[150]

    final = _cadena_completa(contacts, unread=set(), pending={sepultado["phone"]})

    assert sepultado in final, (
        "El contacto con el cliente esperando se pierde en algun punto de la "
        "cadena de cortes y nunca llega a la pantalla de la asesora"
    )


def test_la_senal_llega_viva_al_final_de_la_cadena():
    """
    De nada sirve marcar `pending_reply` si algo aguas abajo reconstruye el
    diccionario del contacto: el panel no recibiria la senal y el aviso no se
    pintaria nunca. Hoy todas las reasignaciones del endpoint son filtros o
    concatenaciones de los mismos objetos; esto lo deja fijado.

    Mutacion: rehacer los contactos con un dict nuevo en cualquier paso deja
    este test en rojo.
    """
    contacts = [
        {"phone": f"+5730000{i:04d}", "in_priority_zset": False}
        for i in range(200)
    ]
    sepultado = contacts[150]

    final = _cadena_completa(contacts, unread=set(), pending={sepultado["phone"]})

    entregado = next(c for c in final if c["phone"] == sepultado["phone"])
    assert entregado.get("pending_reply") is True, (
        "La senal se pierde por el camino: el panel no puede pintar el aviso"
    )


def test_sin_pendientes_la_cadena_devuelve_lo_de_siempre():
    """El caso sano no debe inflarse: sin pendientes, la respuesta es la de antes."""
    contacts = [
        {"phone": f"+5730000{i:04d}", "in_priority_zset": True}
        for i in range(200)
    ]

    final = _cadena_completa(contacts, unread=set(), pending=set())

    assert len(final) == LIMIT


def test_ningun_corte_del_endpoint_ignora_el_criterio():
    """
    Barrido estructural: cada corte de lista del endpoint tiene que consultar un
    predicado de 'nunca se corta' cerca. Si alguien anade un cuarto corte sin el,
    esto rompe — que es exactamente como se escaparon el segundo y el tercero.
    """
    import ast

    with open(panel.__file__, encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
        and n.name == "get_active_contacts"
    )

    PREDICADOS = ("_nunca_se_corta", "_cuenta_como_prioridad", "_split_always_visible")

    # De donde sale cada variable dentro de la funcion.
    origen: dict = {}
    for nodo in ast.walk(fn):
        if isinstance(nodo, ast.Assign):
            for destino in nodo.targets:
                if isinstance(destino, ast.Name):
                    origen[destino.id] = ast.get_source_segment(src, nodo) or ""

    # Todo corte tiene la forma `X = <preservados> + <resto>[:n]`. Lo que importa
    # no es la linea del corte, sino como se construyo la lista preservada.
    cortes = []
    for nodo in ast.walk(fn):
        if not isinstance(nodo, ast.Assign):
            continue
        valor = nodo.value
        if not (isinstance(valor, ast.BinOp) and isinstance(valor.op, ast.Add)):
            continue
        if not (isinstance(valor.right, ast.Subscript) and isinstance(valor.right.slice, ast.Slice)):
            continue

        preservados = valor.left
        fuente = ""
        if isinstance(preservados, ast.Name):
            fuente = origen.get(preservados.id, "")
        cortes.append((
            nodo.lineno,
            ast.get_source_segment(src, nodo) or "",
            any(p in fuente for p in PREDICADOS),
        ))

    assert cortes, "No se encontro ningun corte: cambio la forma del endpoint"
    assert len(cortes) >= 2, f"Se esperaban al menos 2 cortes en el endpoint, hay {len(cortes)}"

    sin_criterio = [(n, l) for n, l, ok in cortes if not ok]
    assert not sin_criterio, (
        "Cortes cuya lista preservada no se construye con un predicado de "
        "'nunca se corta' — los contactos con el cliente esperando se pierden ahi:\n"
        + "\n".join(f"  linea {n}: {l}" for n, l in sin_criterio)
    )


def test_el_predicado_final_mira_las_dos_senales():
    assert _nunca_se_corta({"has_unread": True}) is True
    assert _nunca_se_corta({"pending_rescued": True}) is True
    assert _nunca_se_corta({"has_unread": False, "pending_rescued": False}) is False
    assert _nunca_se_corta({}) is False, "Sin senal no hay pase: el corte normal aplica"


# ── El endpoint sigue cableado a la regla ─────────────────────────────────────


def test_el_endpoint_usa_la_regla_de_pendientes():
    """
    Que la funcion pura funcione no sirve de nada si el endpoint no la llama.

    Mutacion: desconectar _resolve_pending_reply_phones de get_active_contacts
    deja todos los tests de arriba en verde y el bug intacto. Este lo caza.
    """
    import ast

    with open(panel.__file__, encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
        and n.name == "get_active_contacts"
    )
    seg = ast.get_source_segment(src, fn)

    assert "_resolve_pending_reply_phones" in seg, (
        "El endpoint no resuelve los pendientes: la regla no llega a produccion"
    )
    assert "_split_always_visible" in seg, (
        "El endpoint no usa el corte con pendientes"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
