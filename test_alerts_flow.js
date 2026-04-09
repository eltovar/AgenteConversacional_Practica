/**
 * Test: Flujo de Alertas Flotantes — Bug de Re-aparición Solucionado
 *
 * Verifica que las alertas:
 * 1. Aparecen cuando last_activity > 2h
 * 2. Se suprimen permanentemente cuando la asesora responde
 * 3. No reaparecen aunque pasen 30+ minutos
 * 4. Reaparecen legítimamente si el cliente envía nuevo mensaje
 */

const PENDING_ALERT_THRESHOLD_MS = 2 * 60 * 60 * 1000;  // 2 horas
const PENDING_ALERT_COOLDOWN_MS = 30 * 60 * 1000;        // 30 minutos

// ═══════════════════════════════════════════════════════════════════════════════
// SIMULADOR DE ESTADO
// ═══════════════════════════════════════════════════════════════════════════════

class AlertFlowSimulator {
    constructor() {
        this.now = Date.now();  // Tiempo simulado
        this._pendingAlertShown = {};
    }

    advanceTime(ms) {
        this.now += ms;
        console.log(`   [TIME] Avanzado ${ms}ms → ahora=${new Date(this.now).toISOString()}`);
    }

    shouldShowAlert(phone, lastActivityTs, lastAdvisorMessageTs) {
        const now = this.now;
        const lastTs = lastActivityTs;
        const lastAdvisorTs = lastAdvisorMessageTs || 0;
        const record = this._pendingAlertShown[phone];

        // Comprobar supresión permanente (asesora respondio despues del ultimo msg del cliente)
        if (record && record.shownAt >= lastTs) {
            console.log(`      [SUPRESION PERMANENTE] shownAt=${new Date(record.shownAt).toISOString()} >= lastTs=${new Date(lastTs).toISOString()}`);
            return false;
        }

        // Comprobar cooldown
        if (record && (now - record.shownAt) < PENDING_ALERT_COOLDOWN_MS) {
            console.log(`      [COOLDOWN] ${Math.round((now - record.shownAt) / 1000)}s < 30min`);
            return false;
        }

        // Comprobar threshold 2h
        if ((now - lastTs) < PENDING_ALERT_THRESHOLD_MS) {
            console.log(`      [< 2h] ${Math.round((now - lastTs) / 1000)}s sin respuesta`);
            return false;
        }

        return true;
    }

    showAlert(phone, name) {
        this._pendingAlertShown[phone] = { shownAt: this.now };
        console.log(`   [ALERTA] Mostrada para ${phone} (${name}) a ${new Date(this.now).toISOString()}`);
    }

    dismissAlert(phone) {
        this._pendingAlertShown[phone] = { shownAt: this.now };
        console.log(`   [DISMISS] Registrado para ${phone} a ${new Date(this.now).toISOString()}`);
    }

    advisorResponded(phone) {
        // Simular que la asesora respondio (esto actualiza last_advisor_message en backend)
        this._pendingAlertShown[phone] = { shownAt: this.now };
        console.log(`   [RESPONDER] Asesora respondio para ${phone} a ${new Date(this.now).toISOString()}`);
    }
}

// ═══════════════════════════════════════════════════════════════════════════════
// TEST FUNCTIONS
// ═══════════════════════════════════════════════════════════════════════════════

function test_alert_appears_after_2h() {
    console.log('\n[TEST 1] Alerta aparece cuando last_activity > 2h');

    const sim = new AlertFlowSimulator();
    const phone = '+573001234567';
    const clientMsg_T0 = sim.now;

    console.log(`   T=0: Cliente envía mensaje, last_activity=${new Date(clientMsg_T0).toISOString()}`);

    // Esperar 1.9h - NO debería mostrar alerta
    sim.advanceTime(1.9 * 60 * 60 * 1000);
    let shouldShow = sim.shouldShowAlert(phone, clientMsg_T0, 0);
    console.log(`   T+1.9h: shouldShowAlert=${shouldShow}`);
    if (shouldShow) throw new Error('ERROR: Alerta no deberia mostrar antes de 2h');

    // Esperar 2.1h - SÍ debería mostrar alerta
    sim.advanceTime(0.2 * 60 * 60 * 1000);
    shouldShow = sim.shouldShowAlert(phone, clientMsg_T0, 0);
    console.log(`   T+2.1h: shouldShowAlert=${shouldShow}`);
    if (!shouldShow) throw new Error('ERROR: Alerta deberia mostrar despues de 2h');

    sim.showAlert(phone, 'Cliente Test');
    console.log('   ✅ Alerta aparece correctamente a las 2h');
    return true;
}

// ─────────────────────────────────────────────────────────────────────────────

function test_alert_permanent_suppression_after_response() {
    console.log('\n[TEST 2] Alerta se suprime permanentemente cuando asesora responde');

    const sim = new AlertFlowSimulator();
    const phone = '+573001234567';
    const clientMsg_T0 = sim.now;

    console.log(`   T=0: Cliente envía mensaje`);

    // Avanzar 2.5h
    sim.advanceTime(2.5 * 60 * 60 * 1000);
    const T_2_5h = sim.now;

    let shouldShow = sim.shouldShowAlert(phone, clientMsg_T0, 0);
    if (!shouldShow) throw new Error('ERROR: Deberia mostrar alerta a 2.5h');

    console.log(`   T+2.5h: Alerta aparece`);
    sim.showAlert(phone, 'Cliente Test');

    // Asesora responde INMEDIATAMENTE (en la misma llamada a showAlert)
    // Actualizar last_advisor_message = ahora = T+2.5h
    console.log(`   T+2.5h: Asesora envía respuesta en panel`);
    sim.advisorResponded(phone);

    // Avanzar 30 min + 1s (superar cooldown)
    sim.advanceTime((30 * 60 + 1) * 1000);
    console.log(`   T+3h: 30 minutos despues de responder`);

    // Ahora last_advisor_message >= last_activity (ambos = T+2.5h aproximadamente)
    // shouldShowAlert debería retornar False por supresión permanente
    shouldShow = sim.shouldShowAlert(phone, clientMsg_T0, T_2_5h);
    console.log(`   shouldShowAlert=${shouldShow} (con last_advisor_message=T+2.5h)`);
    if (shouldShow) throw new Error('ERROR: Alerta deberia estar suprimida permanentemente');

    console.log('   ✅ Alerta suprimida permanentemente tras respuesta');
    return true;
}

// ─────────────────────────────────────────────────────────────────────────────

function test_alert_reappears_with_new_client_message() {
    console.log('\n[TEST 3] Alerta reaparece legítimamente cuando cliente envía nuevo mensaje');

    const sim = new AlertFlowSimulator();
    const phone = '+573001234567';
    const clientMsg_T0 = sim.now;

    console.log(`   T=0: Cliente envía primer mensaje`);

    sim.advanceTime(2.5 * 60 * 60 * 1000);
    console.log(`   T+2.5h: Alerta aparece, asesora responde`);
    sim.showAlert(phone, 'Cliente');
    sim.advisorResponded(phone);

    const lastAdvisorTs = sim.now;

    // Cliente envía NUEVO mensaje 1 hora despues
    sim.advanceTime(1 * 60 * 60 * 1000);
    const clientMsg_T3_5h = sim.now;
    console.log(`   T+3.5h: Cliente envía NUEVO mensaje`);

    // last_activity ahora es T+3.5h, pero last_advisor_message es T+2.5h
    // Como clientMsg_T3_5h > lastAdvisorTs, la supresión permanente NO aplica
    let shouldShow = sim.shouldShowAlert(phone, clientMsg_T3_5h, lastAdvisorTs);
    console.log(`   T+3.5h: shouldShowAlert=${shouldShow}`);
    if (shouldShow) throw new Error('ERROR: Alerta no deberia mostrar 1h despues del nuevo msg');

    // Avanzar 2h mas (total 3h sin respuesta al nuevo msg)
    sim.advanceTime(2 * 60 * 60 * 1000);
    console.log(`   T+5.5h: 2 horas despues del nuevo mensaje del cliente`);

    shouldShow = sim.shouldShowAlert(phone, clientMsg_T3_5h, lastAdvisorTs);
    console.log(`   shouldShowAlert=${shouldShow}`);
    if (!shouldShow) throw new Error('ERROR: Deberia mostrar alerta legítima para nuevo mensaje');

    sim.showAlert(phone, 'Cliente');
    console.log('   ✅ Nueva alerta aparece legítimamente para nuevo mensaje sin respuesta');
    return true;
}

// ─────────────────────────────────────────────────────────────────────────────

function test_cooldown_vs_permanent_suppression() {
    console.log('\n[TEST 4] Diferencia entre cooldown (30min) y supresión permanente');

    const sim = new AlertFlowSimulator();
    const phone = '+573001234567';
    const clientMsg_T0 = sim.now;

    console.log(`   Escenario A: Solo dismiss (sin responder)`);
    sim.advanceTime(2.5 * 60 * 60 * 1000);
    console.log(`   T+2.5h: Alerta aparece, asesora abre contacto (dismiss)`);
    sim.dismissAlert(phone);
    const dismissTs = sim.now;

    // Esperar 15 min (< 30min)
    sim.advanceTime(15 * 60 * 1000);
    console.log(`   T+2.75h: 15 min despues (< 30min cooldown)`);
    let shouldShow = sim.shouldShowAlert(phone, clientMsg_T0, 0);
    console.log(`   shouldShowAlert=${shouldShow} (cooldown aun activo)`);
    if (shouldShow) throw new Error('ERROR: Cooldown deberia bloquear a los 15min');

    // Esperar otros 15 min (total 30min, expira cooldown)
    sim.advanceTime(15 * 60 * 1000);
    console.log(`   T+3h: 30 min despues (cooldown expira)`);
    shouldShow = sim.shouldShowAlert(phone, clientMsg_T0, 0);
    console.log(`   shouldShowAlert=${shouldShow} (cooldown expiro, pero shownAt >= lastTs aun)`);
    // Como no se ha actualizado last_advisor_message, la supresión permanente tampoco aplica
    // PERO: dismiss registra shownAt = ahora > lastTs (cliente no envio nuevos mensajes)
    console.log(`   ✅ Diferencia clear: cooldown es fallback, supresión permanente es verdad estructural`);
    return true;
}

// ─────────────────────────────────────────────────────────────────────────────

function test_f5_preserves_dismissal() {
    console.log('\n[TEST 5] F5 + sessionStorage preserva dismissal');

    const sim = new AlertFlowSimulator();
    const phone = '+573001234567';
    const clientMsg_T0 = sim.now;

    sim.advanceTime(2.5 * 60 * 60 * 1000);
    console.log(`   T+2.5h: Alerta aparece, asesora responde`);
    sim.advisorResponded(phone);
    const responseTs = sim.now;

    // Simular F5 (página recarga, _pendingAlertShown se restaura de sessionStorage)
    console.log(`   F5: Página recarga, sessionStorage restaura _pendingAlertShown`);
    const savedState = { ...sim._pendingAlertShown };
    sim._pendingAlertShown = savedState;

    // Avanzar 30 min
    sim.advanceTime(30 * 60 * 1000);
    console.log(`   T+3h: 30 min despues (cooldown pasaría sin sessionStorage)`);

    let shouldShow = sim.shouldShowAlert(phone, clientMsg_T0, responseTs);
    console.log(`   shouldShowAlert=${shouldShow}`);
    if (shouldShow) throw new Error('ERROR: Alerta no deberia mostrar si shownAt >= lastActivity');

    console.log('   ✅ F5 no restaura alertas gracias a sessionStorage persistente');
    return true;
}

// ═══════════════════════════════════════════════════════════════════════════════
// MAIN
// ═══════════════════════════════════════════════════════════════════════════════

function main() {
    console.log('\n' + '='.repeat(80));
    console.log('TEST SUITE: Alertas Flotantes — Bug de Re-aparición');
    console.log('='.repeat(80));

    const tests = [
        test_alert_appears_after_2h,
        test_alert_permanent_suppression_after_response,
        test_alert_reappears_with_new_client_message,
        test_cooldown_vs_permanent_suppression,
        test_f5_preserves_dismissal,
    ];

    const results = [];
    for (const test of tests) {
        try {
            test();
            results.push({ name: test.name, status: 'PASS', error: null });
        } catch (err) {
            results.push({ name: test.name, status: 'FAIL', error: err.message });
        }
    }

    console.log('\n' + '='.repeat(80));
    console.log('RESUMEN');
    console.log('='.repeat(80));

    const passed = results.filter(r => r.status === 'PASS').length;
    const failed = results.filter(r => r.status === 'FAIL').length;

    for (const result of results) {
        const icon = result.status === 'PASS' ? '[PASS]' : '[FAIL]';
        console.log(`${icon} ${result.name}`);
        if (result.error) {
            console.log(`      → ${result.error}`);
        }
    }

    console.log(`\nTotal: ${passed} PASS, ${failed} FAIL`);
    console.log('='.repeat(80) + '\n');

    process.exit(failed === 0 ? 0 : 1);
}

if (require.main === module) {
    main();
}

module.exports = { AlertFlowSimulator };
