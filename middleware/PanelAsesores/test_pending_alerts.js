/**
 * TEST SUITE — Alertas Flotantes de Espera
 * ==========================================
 * Verifica que checkPendingResponseAlerts() dispare correctamente la
 * notificación cuando un cliente lleva 2h+ esperando respuesta.
 *
 * INSTRUCCIONES:
 *   1. Abrir el Panel de Asesores en el navegador
 *   2. Abrir DevTools → pestaña Console (F12)
 *   3. Pegar este script completo y presionar Enter
 *   4. Leer los resultados — cada test muestra ✅ PASS o ❌ FAIL
 *
 * El script restaura el estado original al terminar (no altera datos reales).
 */
(async function runPendingAlertTests() {
    'use strict';

    // ── Colores en consola ──────────────────────────────────────────────────
    const PASS  = (msg) => console.log(`%c✅ PASS  ${msg}`, 'color:#22c55e;font-weight:bold');
    const FAIL  = (msg) => console.error(`❌ FAIL  ${msg}`);
    const INFO  = (msg) => console.log(`%cℹ️  ${msg}`, 'color:#64748b');
    const TITLE = (msg) => console.log(`%c\n══ ${msg} ══`, 'color:#f59e0b;font-weight:bold;font-size:13px');

    let passed = 0, failed = 0;

    function assert(condition, description) {
        if (condition) { PASS(description); passed++; }
        else           { FAIL(description); failed++; }
    }

    // ── Helpers ─────────────────────────────────────────────────────────────
    function msAgo(ms)  { return new Date(Date.now() - ms).toISOString(); }
    function hoursAgo(h) { return msAgo(h * 3600 * 1000); }

    function getAlertCard(phone) {
        return document.querySelector(`[data-alert-phone="${phone}"]`);
    }

    function countAlertCards() {
        const c = document.getElementById('pendingAlertsContainer');
        return c ? c.children.length : -1;
    }

    function clearAllAlertCards() {
        const c = document.getElementById('pendingAlertsContainer');
        if (c) c.innerHTML = '';
    }

    // ── Verificar que el entorno es correcto ─────────────────────────────────
    TITLE('Verificación de entorno');
    const envOk = (
        typeof checkPendingResponseAlerts === 'function' &&
        typeof _showPendingResponseAlert   === 'function' &&
        typeof _dismissPendingAlert        === 'function' &&
        typeof _pendingAlertShown          === 'object'   &&
        typeof PENDING_ALERT_THRESHOLD_MS  === 'number'   &&
        typeof PENDING_ALERT_COOLDOWN_MS   === 'number'   &&
        !!document.getElementById('pendingAlertsContainer')
    );
    assert(envOk, 'Funciones y constantes de alertas existen en el scope global');
    if (!envOk) {
        console.error('⛔ El panel no está cargado o el script no se pegó con el panel abierto. Abortando tests.');
        return;
    }
    INFO(`PENDING_ALERT_THRESHOLD_MS = ${PENDING_ALERT_THRESHOLD_MS / 3600000}h`);
    INFO(`PENDING_ALERT_COOLDOWN_MS  = ${PENDING_ALERT_COOLDOWN_MS  / 60000}min`);
    assert(PENDING_ALERT_THRESHOLD_MS === 2 * 60 * 60 * 1000, 'Umbral es exactamente 2 horas');
    assert(PENDING_ALERT_COOLDOWN_MS  === 30 * 60 * 1000,     'Cooldown es exactamente 30 minutos');

    // ── Guardar estado original ──────────────────────────────────────────────
    const _origAllContacts   = allContacts.slice();
    const _origCurrentPhone  = currentPhone;
    const _origAlertShown    = Object.assign({}, _pendingAlertShown);
    clearAllAlertCards();
    Object.keys(_pendingAlertShown).forEach(k => delete _pendingAlertShown[k]);

    // ── TEST 1: Disparador básico ─────────────────────────────────────────────
    TITLE('TEST 1 — Disparador básico (HUMAN_ACTIVE + 3h de espera)');
    {
        allContacts = [{
            phone: '+5730000000001',
            display_name: 'Test Cliente',
            conversation_status: 'HUMAN_ACTIVE',
            last_activity: hoursAgo(3),
            contact_id: 'test_001',
            canal_origen: 'whatsapp'
        }];
        // Asegurar que este teléfono no es el actualmente abierto
        if (typeof currentPhone !== 'undefined') window._savedCurrentPhone = currentPhone;
        // No establecemos currentPhone como este teléfono

        checkPendingResponseAlerts();
        await new Promise(r => setTimeout(r, 50));

        assert(
            countAlertCards() === 1,
            'Aparece 1 carta de alerta en el DOM'
        );
        const card = getAlertCard('+5730000000001');
        assert(!!card, 'La carta tiene data-alert-phone correcto');
        assert(
            card && card.textContent.includes('sigue esperando respuesta'),
            'La carta muestra el texto "sigue esperando respuesta"'
        );
        assert(
            card && card.textContent.includes('Test Cliente'),
            'La carta muestra el nombre del contacto'
        );
        assert(
            card && (card.textContent.includes('3h') || card.textContent.includes('2h')),
            'La carta muestra el tiempo de espera en horas'
        );
        assert(
            !!_pendingAlertShown['+5730000000001'],
            'Se registra en _pendingAlertShown'
        );

        clearAllAlertCards();
        Object.keys(_pendingAlertShown).forEach(k => delete _pendingAlertShown[k]);
    }

    // ── TEST 2: PENDING_HANDOFF también dispara ────────────────────────────
    TITLE('TEST 2 — PENDING_HANDOFF + 2.5h dispara alerta');
    {
        allContacts = [{
            phone: '+5730000000002',
            display_name: 'Test Handoff',
            conversation_status: 'PENDING_HANDOFF',
            last_activity: hoursAgo(2.5),
            contact_id: 'test_002',
            canal_origen: 'whatsapp'
        }];
        checkPendingResponseAlerts();
        await new Promise(r => setTimeout(r, 50));

        assert(countAlertCards() === 1, 'PENDING_HANDOFF + 2.5h genera alerta');
        clearAllAlertCards();
        Object.keys(_pendingAlertShown).forEach(k => delete _pendingAlertShown[k]);
    }

    // ── TEST 3: Menos de 2h NO dispara ────────────────────────────────────
    TITLE('TEST 3 — HUMAN_ACTIVE + solo 1h de espera NO dispara');
    {
        allContacts = [{
            phone: '+5730000000003',
            display_name: 'Test Reciente',
            conversation_status: 'HUMAN_ACTIVE',
            last_activity: hoursAgo(1),
            contact_id: 'test_003',
            canal_origen: 'whatsapp'
        }];
        checkPendingResponseAlerts();
        await new Promise(r => setTimeout(r, 50));

        assert(countAlertCards() === 0, 'Espera de 1h NO genera alerta');
        clearAllAlertCards();
        Object.keys(_pendingAlertShown).forEach(k => delete _pendingAlertShown[k]);
    }

    // ── TEST 4: BOT_ACTIVE NO dispara ─────────────────────────────────────
    TITLE('TEST 4 — BOT_ACTIVE con 5h de espera NO dispara');
    {
        allContacts = [{
            phone: '+5730000000004',
            display_name: 'Test Bot',
            conversation_status: 'BOT_ACTIVE',
            last_activity: hoursAgo(5),
            contact_id: 'test_004',
            canal_origen: 'whatsapp'
        }];
        checkPendingResponseAlerts();
        await new Promise(r => setTimeout(r, 50));

        assert(countAlertCards() === 0, 'BOT_ACTIVE NO genera alerta aunque lleve 5h');
        clearAllAlertCards();
        Object.keys(_pendingAlertShown).forEach(k => delete _pendingAlertShown[k]);
    }

    // ── TEST 5: IN_CONVERSATION NO dispara ────────────────────────────────
    TITLE('TEST 5 — IN_CONVERSATION NO dispara (hay intercambio activo)');
    {
        allContacts = [{
            phone: '+5730000000005',
            display_name: 'Test En Conversacion',
            conversation_status: 'IN_CONVERSATION',
            last_activity: hoursAgo(3),
            contact_id: 'test_005',
            canal_origen: 'whatsapp'
        }];
        checkPendingResponseAlerts();
        await new Promise(r => setTimeout(r, 50));

        assert(countAlertCards() === 0, 'IN_CONVERSATION NO genera alerta');
        clearAllAlertCards();
        Object.keys(_pendingAlertShown).forEach(k => delete _pendingAlertShown[k]);
    }

    // ── TEST 6: Contacto actualmente abierto NO dispara ───────────────────
    TITLE('TEST 6 — Contacto actualmente seleccionado (currentPhone) NO dispara');
    {
        const testPhone = '+5730000000006';
        const _prevCurrentPhone = currentPhone;
        currentPhone = testPhone;

        allContacts = [{
            phone: testPhone,
            display_name: 'Test Abierto',
            conversation_status: 'HUMAN_ACTIVE',
            last_activity: hoursAgo(4),
            contact_id: 'test_006',
            canal_origen: 'whatsapp'
        }];
        checkPendingResponseAlerts();
        await new Promise(r => setTimeout(r, 50));

        assert(countAlertCards() === 0, 'Contacto actualmente abierto NO genera alerta');
        currentPhone = _prevCurrentPhone;
        clearAllAlertCards();
        Object.keys(_pendingAlertShown).forEach(k => delete _pendingAlertShown[k]);
    }

    // ── TEST 7: Cooldown — segunda llamada inmediata no re-muestra ────────
    TITLE('TEST 7 — Cooldown: misma alerta no aparece dos veces en 30 min');
    {
        const testPhone = '+5730000000007';
        allContacts = [{
            phone: testPhone,
            display_name: 'Test Cooldown',
            conversation_status: 'HUMAN_ACTIVE',
            last_activity: hoursAgo(3),
            contact_id: 'test_007',
            canal_origen: 'whatsapp'
        }];

        checkPendingResponseAlerts(); // Primera llamada → muestra alerta
        await new Promise(r => setTimeout(r, 50));
        assert(countAlertCards() === 1, 'Primera llamada: alerta aparece');

        clearAllAlertCards(); // Simular que el usuario descartó la carta (DOM limpio)
        // Pero _pendingAlertShown[phone].shownAt recién se registró

        checkPendingResponseAlerts(); // Segunda llamada inmediata
        await new Promise(r => setTimeout(r, 50));
        assert(countAlertCards() === 0, 'Segunda llamada inmediata: cooldown activo, NO reaparece');

        clearAllAlertCards();
        Object.keys(_pendingAlertShown).forEach(k => delete _pendingAlertShown[k]);
    }

    // ── TEST 8: Dismiss limpia la carta y establece cooldown ─────────────
    TITLE('TEST 8 — Dismiss: elimina carta del DOM y establece cooldown');
    {
        const testPhone = '+5730000000008';
        allContacts = [{
            phone: testPhone,
            display_name: 'Test Dismiss',
            conversation_status: 'HUMAN_ACTIVE',
            last_activity: hoursAgo(3),
            contact_id: 'test_008',
            canal_origen: 'whatsapp'
        }];

        checkPendingResponseAlerts();
        await new Promise(r => setTimeout(r, 50));
        assert(countAlertCards() === 1, 'Alerta mostrada antes del dismiss');

        const beforeDismiss = Date.now();
        _dismissPendingAlert(testPhone);
        await new Promise(r => setTimeout(r, 400)); // Esperar animación (300ms)

        assert(countAlertCards() === 0, 'Carta eliminada del DOM tras dismiss');
        assert(
            !!_pendingAlertShown[testPhone] &&
            _pendingAlertShown[testPhone].shownAt >= beforeDismiss,
            'Cooldown registrado con timestamp de dismiss'
        );

        // Verificar que no reaparece
        checkPendingResponseAlerts();
        await new Promise(r => setTimeout(r, 50));
        assert(countAlertCards() === 0, 'Tras dismiss, no reaparece en siguientes checks');

        clearAllAlertCards();
        Object.keys(_pendingAlertShown).forEach(k => delete _pendingAlertShown[k]);
    }

    // ── TEST 9: _dismissPendingAlert desde selectContact establece cooldown ──
    TITLE('TEST 9 — Abrir contacto sin alerta visible igualmente establece cooldown');
    {
        const testPhone = '+5730000000009';
        // No hay alerta mostrada, pero el contacto está activo
        assert(!_pendingAlertShown[testPhone], 'Sin registro previo en _pendingAlertShown');

        const beforeCall = Date.now();
        _dismissPendingAlert(testPhone); // Simula lo que hace selectContact()

        assert(
            !!_pendingAlertShown[testPhone] &&
            _pendingAlertShown[testPhone].shownAt >= beforeCall,
            'selectContact establece cooldown incluso sin alerta previa'
        );

        // Verificar que no aparecerá alerta por 30 min
        allContacts = [{
            phone: testPhone,
            display_name: 'Test No Alert',
            conversation_status: 'HUMAN_ACTIVE',
            last_activity: hoursAgo(4),
            contact_id: 'test_009',
            canal_origen: 'whatsapp'
        }];
        checkPendingResponseAlerts();
        await new Promise(r => setTimeout(r, 50));
        assert(countAlertCards() === 0, 'Contacto visto por el asesor no genera alerta inmediata');

        clearAllAlertCards();
        Object.keys(_pendingAlertShown).forEach(k => delete _pendingAlertShown[k]);
    }

    // ── TEST 10: Máximo 5 alertas simultáneas ────────────────────────────
    TITLE('TEST 10 — Máximo 5 alertas simultáneas');
    {
        allContacts = Array.from({ length: 7 }, (_, i) => ({
            phone: `+573000000010${i}`,
            display_name: `Cliente ${i + 1}`,
            conversation_status: 'HUMAN_ACTIVE',
            last_activity: hoursAgo(3 + i),
            contact_id: `test_01${i}`,
            canal_origen: 'whatsapp'
        }));
        checkPendingResponseAlerts();
        await new Promise(r => setTimeout(r, 50));

        assert(countAlertCards() === 5, 'Con 7 contactos pendientes, solo se muestran 5 alertas');
        clearAllAlertCards();
        Object.keys(_pendingAlertShown).forEach(k => delete _pendingAlertShown[k]);
    }

    // ── TEST 11: Exactamente en el límite de 2h (edge case) ───────────────
    TITLE('TEST 11 — Edge case: exactamente 2 horas (sin 1ms más) NO dispara');
    {
        // last_activity = exactamente PENDING_ALERT_THRESHOLD_MS atrás
        // now - lastTs === PENDING_ALERT_THRESHOLD_MS → condición es < (no <=), no dispara
        const exactlyAtThreshold = new Date(Date.now() - PENDING_ALERT_THRESHOLD_MS).toISOString();
        allContacts = [{
            phone: '+5730000000099',
            display_name: 'Test Limite Exacto',
            conversation_status: 'HUMAN_ACTIVE',
            last_activity: exactlyAtThreshold,
            contact_id: 'test_099',
            canal_origen: 'whatsapp'
        }];
        checkPendingResponseAlerts();
        await new Promise(r => setTimeout(r, 50));

        // (now - lastTs) === PENDING_ALERT_THRESHOLD_MS → NO dispara (condición es estrictamente <)
        assert(countAlertCards() === 0, 'Exactamente en el umbral no dispara (la condición es >2h, no >=2h)');
        clearAllAlertCards();
        Object.keys(_pendingAlertShown).forEach(k => delete _pendingAlertShown[k]);
    }

    // ── TEST 12: Más de 2h + 1ms SÍ dispara ──────────────────────────────
    TITLE('TEST 12 — Edge case: 2h + 1 segundo SÍ dispara');
    {
        allContacts = [{
            phone: '+5730000000098',
            display_name: 'Test Despues Del Umbral',
            conversation_status: 'HUMAN_ACTIVE',
            last_activity: new Date(Date.now() - PENDING_ALERT_THRESHOLD_MS - 1000).toISOString(),
            contact_id: 'test_098',
            canal_origen: 'whatsapp'
        }];
        checkPendingResponseAlerts();
        await new Promise(r => setTimeout(r, 50));

        assert(countAlertCards() === 1, '2h + 1s SÍ dispara la alerta');
        clearAllAlertCards();
        Object.keys(_pendingAlertShown).forEach(k => delete _pendingAlertShown[k]);
    }

    // ── TEST 13: Contacto sin last_activity no dispara ────────────────────
    TITLE('TEST 13 — Contacto sin last_activity NO dispara (dato faltante)');
    {
        allContacts = [{
            phone: '+5730000000097',
            display_name: 'Test Sin Fecha',
            conversation_status: 'HUMAN_ACTIVE',
            last_activity: null,
            contact_id: 'test_097',
            canal_origen: 'whatsapp'
        }];
        checkPendingResponseAlerts();
        await new Promise(r => setTimeout(r, 50));

        assert(countAlertCards() === 0, 'Sin last_activity, no genera alerta (evita NaN)');
        clearAllAlertCards();
        Object.keys(_pendingAlertShown).forEach(k => delete _pendingAlertShown[k]);
    }

    // ── TEST 14: Verificar contenido HTML de la carta ─────────────────────
    TITLE('TEST 14 — Estructura HTML de la carta de alerta');
    {
        allContacts = [{
            phone: '+5730000000096',
            display_name: 'Maria Gonzalez',
            conversation_status: 'HUMAN_ACTIVE',
            last_activity: hoursAgo(3.5),
            contact_id: 'test_096',
            canal_origen: 'whatsapp'
        }];
        checkPendingResponseAlerts();
        await new Promise(r => setTimeout(r, 50));

        const card = getAlertCard('+5730000000096');
        assert(!!card, 'Carta creada en el DOM');
        assert(
            card && card.querySelector('[data-dismiss-phone]'),
            'Botón de dismiss presente en la carta'
        );
        assert(
            card && card.textContent.includes('MG'),
            'Iniciales "MG" de "Maria Gonzalez" en el avatar'
        );
        assert(
            card && card.textContent.includes('3h'),
            'Tiempo de espera muestra horas'
        );
        clearAllAlertCards();
        Object.keys(_pendingAlertShown).forEach(k => delete _pendingAlertShown[k]);
    }

    // ── Restaurar estado original ────────────────────────────────────────────
    allContacts = _origAllContacts;
    currentPhone = _origCurrentPhone;
    Object.keys(_pendingAlertShown).forEach(k => delete _pendingAlertShown[k]);
    Object.assign(_pendingAlertShown, _origAlertShown);
    clearAllAlertCards();

    // ── Resumen ─────────────────────────────────────────────────────────────
    console.log('');
    console.log(
        `%c══════════════════════════════════════\n  RESULTADOS: ${passed} ✅ PASS   ${failed} ❌ FAIL\n══════════════════════════════════════`,
        failed === 0
            ? 'color:#22c55e;font-weight:bold;font-size:14px'
            : 'color:#ef4444;font-weight:bold;font-size:14px'
    );
    if (failed > 0) {
        console.warn('⚠️  Revisar los tests en rojo arriba antes de hacer deploy.');
    } else {
        console.log('%c🚀 Todos los tests pasaron. Listo para deploy.', 'color:#22c55e;font-weight:bold');
    }
})();
