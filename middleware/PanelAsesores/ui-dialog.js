/**
 * ui-dialog.js — Modales de decisión del panel de asesoras.
 *
 * Reemplaza confirm() y prompt() nativos, que el navegador dibuja con su propio
 * estilo (imposible de tematizar) y que bloquean el hilo del panel.
 *
 * API:
 *   await confirmDialog({ title, message, bullets, tone, confirmLabel, cancelLabel })
 *       -> Promise<boolean>
 *   await choiceDialog({ title, message, bullets, tone, actions: [{id,label,tone}] })
 *       -> Promise<string|null>   (null = cancelado)
 *   await promptDialog({ title, message, label, defaultValue, tone, confirmLabel })
 *       -> Promise<string|null>   (null = cancelado)
 *
 * tone: 'danger' | 'warning' | 'info' | 'success'. Define franja, icono y botón.
 * Los estilos viven en style.css (.dlg-*) y no como clases dinámicas de Tailwind:
 * el escáner solo detecta literales y `dlg-accent-${tone}` se purgaría del build.
 *
 * OLA 2 (pendiente, decidida pero no implementada): los 52 alert() pasan a
 * toast para éxito (showToast ya existe) y a un alertDialog de un botón para
 * error. Hasta entonces esos avisos siguen siendo nativos.
 */

(function () {
    'use strict';

    const TONE_ICON = {
        danger:  '⚠',   // ⚠
        warning: '⚠',
        info:    'ℹ',   // ℹ
        success: '✓',   // ✓
    };

    let _activeDialog = null;   // { overlay, resolve, previousFocus }

    function _isOpen() {
        return _activeDialog !== null;
    }

    /** Cierra el diálogo abierto y resuelve con `value`. Idempotente. */
    function _close(value) {
        if (!_activeDialog) return;
        const { overlay, resolve, previousFocus } = _activeDialog;
        _activeDialog = null;
        document.removeEventListener('keydown', _onKeydown, true);
        overlay.remove();
        if (previousFocus && typeof previousFocus.focus === 'function') {
            try { previousFocus.focus(); } catch (e) { /* elemento ya no existe */ }
        }
        resolve(value);
    }

    /**
     * ESC cancela; Tab queda atrapado dentro del panel.
     * Se registra en fase de captura para ganarle al handler global de ESC
     * de index.js, que deselecciona el contacto activo.
     */
    function _onKeydown(e) {
        if (!_activeDialog) return;

        if (e.key === 'Escape') {
            e.preventDefault();
            e.stopPropagation();
            _close(_activeDialog.cancelValue);
            return;
        }

        if (e.key !== 'Tab') return;

        const focusables = _activeDialog.overlay.querySelectorAll(
            'button, input, select, textarea, [tabindex]:not([tabindex="-1"])'
        );
        if (!focusables.length) return;
        const first = focusables[0];
        const last = focusables[focusables.length - 1];

        if (e.shiftKey && document.activeElement === first) {
            e.preventDefault();
            last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
            e.preventDefault();
            first.focus();
        }
    }

    /**
     * Construye y muestra el modal. Núcleo compartido por las 3 primitivas.
     * @returns {Promise<*>} el valor con el que se cerró
     */
    function _open({ title, message, bullets, tone, actions, input, cancelValue }) {
        // Un solo diálogo a la vez: si ya hay uno, se cancela antes de abrir.
        if (_isOpen()) _close(_activeDialog.cancelValue);

        const safeTone = TONE_ICON[tone] ? tone : 'info';

        const overlay = document.createElement('div');
        overlay.className = 'dlg-overlay';
        overlay.setAttribute('role', 'dialog');
        overlay.setAttribute('aria-modal', 'true');

        const titleId = 'dlgTitle_' + Date.now();
        overlay.setAttribute('aria-labelledby', titleId);

        const panel = document.createElement('div');
        panel.className = 'dlg-panel';

        const accent = document.createElement('div');
        accent.className = 'dlg-accent dlg-accent-' + safeTone;
        panel.appendChild(accent);

        const body = document.createElement('div');
        body.className = 'dlg-body';

        const head = document.createElement('div');
        head.className = 'dlg-head';

        const icon = document.createElement('div');
        icon.className = 'dlg-icon dlg-icon-' + safeTone;
        icon.setAttribute('aria-hidden', 'true');
        icon.textContent = TONE_ICON[safeTone];
        head.appendChild(icon);

        const textWrap = document.createElement('div');
        textWrap.style.flex = '1';
        textWrap.style.minWidth = '0';

        const h = document.createElement('h2');
        h.className = 'dlg-title';
        h.id = titleId;
        h.textContent = title || 'Confirmar';
        textWrap.appendChild(h);

        if (message) {
            const p = document.createElement('p');
            p.className = 'dlg-message';
            p.textContent = message;
            textWrap.appendChild(p);
        }

        head.appendChild(textWrap);
        body.appendChild(head);

        if (Array.isArray(bullets) && bullets.length) {
            const ul = document.createElement('ul');
            ul.className = 'dlg-bullets';
            bullets.forEach(function (b) {
                const li = document.createElement('li');
                li.textContent = b;
                ul.appendChild(li);
            });
            body.appendChild(ul);
        }

        let inputEl = null;
        if (input) {
            inputEl = document.createElement('input');
            inputEl.type = 'text';
            inputEl.className = 'dlg-input';
            inputEl.value = input.defaultValue != null ? String(input.defaultValue) : '';
            if (input.label) inputEl.setAttribute('aria-label', input.label);
            if (input.placeholder) inputEl.placeholder = input.placeholder;
            inputEl.addEventListener('keydown', function (e) {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    _close(inputEl.value);
                }
            });
            body.appendChild(inputEl);
        }

        panel.appendChild(body);

        const actionsWrap = document.createElement('div');
        actionsWrap.className = 'dlg-actions';

        actions.forEach(function (a) {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'dlg-btn dlg-btn-' + (a.variant || 'info');
            btn.textContent = a.label;
            btn.addEventListener('click', function () {
                _close(a.value !== undefined ? a.value : (inputEl ? inputEl.value : a.id));
            });
            actionsWrap.appendChild(btn);
        });

        panel.appendChild(actionsWrap);
        overlay.appendChild(panel);

        // Clic en el fondo = cancelar. Se comprueba el target para no cerrar
        // cuando el clic nace dentro del panel y termina fuera al soltar.
        overlay.addEventListener('mousedown', function (e) {
            if (e.target === overlay) _close(cancelValue);
        });

        document.body.appendChild(overlay);

        return new Promise(function (resolve) {
            _activeDialog = {
                overlay: overlay,
                resolve: resolve,
                previousFocus: document.activeElement,
                cancelValue: cancelValue,
            };
            document.addEventListener('keydown', _onKeydown, true);

            // Foco inicial: el input si lo hay, si no la acción primaria (última).
            if (inputEl) {
                inputEl.focus();
                inputEl.select();
            } else {
                const btns = actionsWrap.querySelectorAll('button');
                if (btns.length) btns[btns.length - 1].focus();
            }
        });
    }

    /**
     * Confirmación binaria. Sustituto directo de confirm().
     * @returns {Promise<boolean>}
     */
    function confirmDialog(opts) {
        opts = opts || {};
        const tone = opts.tone || 'warning';
        return _open({
            title: opts.title,
            message: opts.message,
            bullets: opts.bullets,
            tone: tone,
            cancelValue: false,
            actions: [
                { id: 'cancel', label: opts.cancelLabel || 'Cancelar', variant: 'cancel', value: false },
                { id: 'ok', label: opts.confirmLabel || 'Confirmar', variant: tone, value: true },
            ],
        });
    }

    /**
     * Dos o más acciones con nombre propio. Para decisiones que NO son
     * confirmar/cancelar y que con confirm() quedaban ambiguas (el caso del
     * audio: "enviar ahora" vs "agregar texto antes").
     * @returns {Promise<string|null>} id de la acción, o null si se cancela
     */
    function choiceDialog(opts) {
        opts = opts || {};
        const actions = (opts.actions || []).map(function (a) {
            return { id: a.id, label: a.label, variant: a.tone || 'info', value: a.id };
        });
        if (opts.cancelLabel !== null) {
            actions.unshift({
                id: 'cancel', label: opts.cancelLabel || 'Cancelar',
                variant: 'cancel', value: null,
            });
        }
        return _open({
            title: opts.title,
            message: opts.message,
            bullets: opts.bullets,
            tone: opts.tone || 'info',
            cancelValue: null,
            actions: actions,
        });
    }

    /**
     * Entrada de texto. Sustituto de prompt().
     * @returns {Promise<string|null>} null si se cancela
     */
    function promptDialog(opts) {
        opts = opts || {};
        return _open({
            title: opts.title,
            message: opts.message,
            tone: opts.tone || 'info',
            cancelValue: null,
            input: {
                defaultValue: opts.defaultValue,
                label: opts.label,
                placeholder: opts.placeholder,
            },
            actions: [
                { id: 'cancel', label: opts.cancelLabel || 'Cancelar', variant: 'cancel', value: null },
                { id: 'ok', label: opts.confirmLabel || 'Aceptar', variant: opts.tone || 'info' },
            ],
        });
    }

    window.confirmDialog = confirmDialog;
    window.choiceDialog = choiceDialog;
    window.promptDialog = promptDialog;
})();
