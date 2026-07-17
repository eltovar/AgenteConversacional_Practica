// =========================================================================
// DATE UTILITIES — Separadores de fecha en el chat
// =========================================================================

const MESES_ES = ['enero','febrero','marzo','abril','mayo','junio',
                  'julio','agosto','septiembre','octubre','noviembre','diciembre'];
const DIAS_ES  = ['domingo','lunes','martes','miércoles','jueves','viernes','sábado'];

/** Retorna "YYYY-MM-DD" del timestamp en zona Bogotá (para comparar días). */
function formatBogotaDate(ts) {
    try {
        if (!ts) return '';
        let safeTs = ts.trim();
        if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/.test(safeTs) && !safeTs.endsWith('Z') && !/[+-]\d{2}:\d{2}$/.test(safeTs)) {
            safeTs += 'Z';
        }
        return new Date(safeTs).toLocaleDateString('en-CA', { timeZone: 'America/Bogota' }); // "YYYY-MM-DD"
    } catch (e) { return ''; }
}

/** Retorna etiqueta legible: "Hoy" | "Ayer" | "Lunes" | "21 de marzo" | "21 de marzo de 2024" */
function getDateLabel(ts) {
    const dateStr = formatBogotaDate(ts);
    if (!dateStr) return '';
    const today = formatBogotaDate(new Date().toISOString());
    if (dateStr === today) return 'Hoy';
    const yesterday = formatBogotaDate(new Date(Date.now() - 86400000).toISOString());
    if (dateStr === yesterday) return 'Ayer';
    const [year, month, day] = dateStr.split('-').map(Number);
    // Construir fecha al mediodía Bogotá para evitar edge cases de timezone
    const msgDate  = new Date(`${dateStr}T12:00:00`);
    const todayDate = new Date(`${today}T12:00:00`);
    const diffDays  = Math.round((todayDate - msgDate) / 86400000);
    if (diffDays < 7) {
        // Últimos 6 días → nombre del día
        const nombreDia = DIAS_ES[msgDate.getDay()];
        return nombreDia.charAt(0).toUpperCase() + nombreDia.slice(1);
    }
    const todayYear = new Date().getFullYear();
    const mes = MESES_ES[month - 1];
    return year === todayYear ? `${day} de ${mes}` : `${day} de ${mes} de ${year}`;
}

/** HTML del separador de fecha estilo WhatsApp. */
function buildDateDivider(label, dateStr) {
    const display = label.charAt(0).toUpperCase() + label.slice(1);
    return `<div class="date-divider flex items-center py-2 px-2" data-date="${dateStr}" data-label="${display}">
        <div class="flex-1 h-px bg-gray-400 opacity-30"></div>
        <span class="mx-3 text-xs text-gray-500 font-medium whitespace-nowrap">${display}</span>
        <div class="flex-1 h-px bg-gray-400 opacity-30"></div>
    </div>`;
}

// =========================================================================
// Formatea la hora en zona Bogotá y con AM/PM claro y robusto
function formatBogotaTime(ts) {
    try {
        if (!ts) return '';
        // Forzar que el string termine en 'Z' (UTC) si no la tiene
        let safeTs = ts.trim();
        if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/.test(safeTs) && !safeTs.endsWith('Z') && !/[+-]\d{2}:\d{2}$/.test(safeTs)) {
            safeTs += 'Z';
        }
        // Parsear como UTC
        const date = new Date(safeTs);
        // Convertir a hora de Bogotá
        return date.toLocaleTimeString('es-CO', {
            timeZone: 'America/Bogota',
            hour: '2-digit',
            minute: '2-digit',
            hour12: true
        });
    } catch (e) {
        return '';
    }
}
// CONFIGURACION
// =========================================================================
// Las variables API_KEY, BASE_URL y ADVISOR_NAMES son inyectadas desde index.html

const POLLING_INTERVAL_IDLE = 10000;   // 10 segundos cuando no hay chat activo
const POLLING_INTERVAL_ACTIVE = 10000;  // 10 segundos — WS maneja eventos en tiempo real (era 3s → 429s)

// Etapas del Pipeline de HubSpot (Contact-based)
const PIPELINE_STAGES = [
    { id: "1326623075", name: "En conversacion" },
    { id: "marketingqualifiedlead", name: "Visita agendada" },
    { id: "salesqualifiedlead", name: "Visita realizada" },
    { id: "opportunity", name: "En estudio" },
    { id: "customer", name: "Cerrado ganado" },
    { id: "evangelist", name: "Cerrado perdido" },
    { id: "other", name: "No responde" },
    { id: "1326623067", name: "Hasta 1.5M" },
    { id: "1326631573", name: "Hasta 2M" },
    { id: "1326632625", name: "Hasta 2.5M" },
    { id: "1326631574", name: "De 3M en adelante" },
    { id: "1326623539", name: "Local o Bodega" },
    { id: "1326632628", name: "Ana Contratos" },
    { id: "1326623069", name: "Propietarios" },
    { id: "1326632209", name: "Pagos y Servicios Publicos" },
    { id: "1326623541", name: "Ya encontro" },
    { id: "subscriber", name: "Reubicados" },
    { id: "lead", name: "Aprobado" },
    { id: "1353539189", name: "Venta" }
];

// Todos los portales disponibles — todos los asesores pueden seleccionar cualquier portal al crear un contacto.
// La asignación del contacto se determina por advisor_id (quien crea), no por el canal seleccionado.
const ALL_PORTALS = [
    { value: "metrocuadrado",    label: "MetroCuadrado" },
    { value: "finca_raiz",       label: "Finca Raíz" },
    { value: "facebook",         label: "Facebook" },
    { value: "instagram",        label: "Instagram" },
    { value: "ciencuadras",      label: "Ciencuadras" },
    { value: "tiktok",           label: "TikTok" },
    { value: "charly",           label: "Charly" },
    { value: "pagina_web",       label: "Página Web" },
    { value: "whatsapp_directo", label: "WhatsApp Directo" },
    { value: "youtube",          label: "YouTube" },
];

// Mapeo advisor ID → portales. Todos los IDs usan la lista completa.
const ADVISOR_PORTALS = {
    "89096378": ALL_PORTALS,
    "89096380": ALL_PORTALS,
};

// Leer parametro advisor de la URL y persistir en sessionStorage para sobrevivir F5/reloads.
// sessionStorage (no localStorage) para que cada pestaña/sesión sea independiente.
const urlParams = new URLSearchParams(window.location.search);
const _rawAdvisorId = urlParams.get('advisor');
if (_rawAdvisorId) {
    sessionStorage.setItem('panel_advisor_id', _rawAdvisorId);
}
const ADVISOR_ID = _rawAdvisorId || sessionStorage.getItem('panel_advisor_id');

const ADVISOR_NAME = ADVISOR_ID ? (ADVISOR_NAMES[ADVISOR_ID] || `Asesor ${ADVISOR_ID}`) : null;

let currentContactId = null;
let currentPhone = null;
let currentCanal = null;  // Canal de origen para segregacion
let currentName = null;   // Nombre del contacto activo para modales
let pollingInterval = null;
let templatesData = [];  // Almacena templates cargados
let allContacts = [];    // Cache de contactos para el buscador
let selectedMediaFile = null;  // Archivo multimedia seleccionado
let replyToMessage = null;  // { id, sender, sender_name, content, media_type, timestamp }
let contactDealCache = {};  // Cache de deal_id por contacto para evitar flickering
const _contactFingerprints = new Map(); // Fingerprint del último render por phone → evita re-renders innecesarios
const recentlyClosedPhones = new Set(); // Guard contra race condition: evita que polling stale re-renderice contactos recién cerrados
// [Bug2] Render guard: evita que dos renders DOM corran intercalados (causa index jumping)
let _renderInProgress = false;
let _pendingRenderContacts = null;

// Contador de mensajes no leídos por telefono (se reinicia en cada sesión)
let unreadCounts = {};
// Timestamps del último last_activity conocido por phone (para detección de nuevos mensajes en polling)
let _lastContactTimestamps = {};
// Fix CR-4: timestamp del último evento WS recibido por phone.
// Evita double-counting cuando el polling detecta el mismo mensaje que el WS ya notificó.
const _lastWsNotifiedTimestamps = {};
// Phones que el asesor abrió en esta sesión.
// Previene re-inicialización de badges desde has_unread (backend) si el asesor ya los limpió.
const _seenPhones = new Set();
// Sistema de notificaciones del asesor (backend-driven)
const NOTIF_POLL_INTERVAL = 30_000; // 30 segundos
let _seenNotifIds = new Set();      // IDs ya procesados (para detectar nuevas)
let _notifDropdownOpen = false;
// "Marcar como no leído" manual — persiste en localStorage entre sesiones
let _manuallyUnread = new Set(
    (() => { try { return JSON.parse(localStorage.getItem('_manuallyUnread') || '[]'); } catch { return []; } })()
);
function _saveManuallyUnread() {
    try { localStorage.setItem('_manuallyUnread', JSON.stringify([..._manuallyUnread])); } catch {}
}
// Flag para saber si ya se ejecutó el auto-select de deep link
let deepLinkHandled = false;

// Template picker (slash command)
let activeTemplateId = null;
let activeTemplateBody = '';
let activeTemplateVars = [];
let pickerSelectedIndex = -1;
let pickerVisibleItems = [];

// Estado de ventana 24h
let currentWindowOpen = true;

// =========================================================================
// ⚠️ 2026-06-02: PAGINACIÓN CURSOR-BASED DEL HISTORIAL (scroll infinito)
// =========================================================================
// Tamaño de página: 100 mensajes por request. Calibrado para:
//   • Carga inicial rápida (~300-500ms para conversaciones típicas).
//   • Suficiente contexto sin saturar DOM (100 burbujas ≈ 500KB DOM).
//   • Bajo costo MongoDB: query con índice phone_timestamp_idx = O(log N).
const CHAT_PAGE_SIZE = 100;

// Trigger del scroll: cargar más cuando scrollTop < SCROLL_THRESHOLD_PX.
// Margen generoso (200px) → la siguiente página llega antes de que el usuario
// llegue al límite visual = sin "salto" perceptible.
const SCROLL_THRESHOLD_PX = 200;

// Throttle del listener scroll → máximo 5 evaluaciones/segundo.
// Combinado con state.loading reduce a máximo 1 fetch real/segundo.
const SCROLL_THROTTLE_MS = 200;

// State global de paginación del historial. Una sola conversación abierta a la vez,
// así que un objeto global basta. Se RESETEA al cambiar de contacto.
const chatHistoryState = {
    contactId: null,        // contact_id de la conversación activa
    canal: null,            // canal asociado (para preservar en paginación)
    phone: null,            // teléfono asociado
    oldestTs: null,         // cursor: timestamp del mensaje más antiguo cargado
    hasMore: true,          // si el backend dijo que aún hay mensajes más antiguos
    loading: false,         // flag anti-doble-fetch (request en vuelo)
    listenerAttached: false,// si ya montamos el scroll listener al contenedor
    scrollTimeout: null,    // handle del setTimeout del throttle
};

function _resetChatHistoryState(contactId, canal, phone) {
    chatHistoryState.contactId = contactId || null;
    chatHistoryState.canal = canal || null;
    chatHistoryState.phone = phone || null;
    chatHistoryState.oldestTs = null;
    chatHistoryState.hasMore = true;
    chatHistoryState.loading = false;
    if (chatHistoryState.scrollTimeout) {
        clearTimeout(chatHistoryState.scrollTimeout);
        chatHistoryState.scrollTimeout = null;
    }
    // Limpiar cache de mensajes — pertenece a la conversación que se cierra.
    window.__chatMessagesCache = [];
    // Quitar marcador de "inicio de conversación" si quedó de la conversación anterior.
    const oldMarker = document.getElementById('chatHistoryStartMarker');
    if (oldMarker) oldMarker.remove();
}

// =========================================================================
// HELPER: UI según estado de ventana 24h
// =========================================================================
function _applyWindowClosedUI(isClosed) {
    const triggerBtn = document.getElementById('templateTriggerBtn');
    const attachBtn  = document.getElementById('attachBtn');
    const recordBtn  = document.getElementById('recordBtn');
    currentWindowOpen = !isClosed;
    if (isClosed) {
        if (triggerBtn) triggerBtn.classList.remove('hidden');
        if (attachBtn)  attachBtn.classList.add('hidden');
        if (recordBtn)  recordBtn.classList.add('hidden');
    } else {
        if (triggerBtn) triggerBtn.classList.add('hidden');
        if (attachBtn)  attachBtn.classList.remove('hidden');
        if (recordBtn)  recordBtn.classList.remove('hidden');
    }
}

// =========================================================================
// LOADER GLOBAL (UX)
// =========================================================================
function showLoader() {
    const loader = document.getElementById('globalLoader');
    if (loader) loader.classList.remove('hidden');
}

function hideLoader() {
    const loader = document.getElementById('globalLoader');
    if (loader) loader.classList.add('hidden');
}

// =========================================================================
// FUNCION DE ACTUALIZACION DE ETAPA DE PIPELINE
// =========================================================================

// =========================================================================
// FUNCION DE BUSQUEDA DE CONTACTOS
// =========================================================================

// Debounce para búsqueda en servidor (evitar spam de requests)
let searchDebounceTimeout = null;

// Estado global: término actual de búsqueda (para empty-state contextual)
window._currentSearchTerm = '';

/**
 * Detecta si el término ingresado parece un teléfono y lo normaliza a E.164.
 * Retorna null si no parece teléfono.
 * Acepta: "3138405930", "+573138405930", "57 313 840 5930", "313-840-5930"
 */
function _detectPhoneTerm(term) {
    if (!term) return null;
    // Extraer solo dígitos y el + inicial si existe
    const hasPlus = term.trim().startsWith('+');
    const digits = term.replace(/[^\d]/g, '');
    if (!digits) return null;
    // Un teléfono válido tiene entre 10 y 15 dígitos
    if (digits.length < 10 || digits.length > 15) return null;
    // Si tiene + explícito o 11+ dígitos → asumir ya tiene código de país
    if (hasPlus || digits.length >= 11) {
        return '+' + digits;
    }
    // 10 dígitos → asumir Colombia (+57)
    if (digits.length === 10) {
        return '+57' + digits;
    }
    return null;
}

async function filterContacts(searchTerm) {
    const term = searchTerm.toLowerCase().trim();
    window._currentSearchTerm = term;

    if (!term) {
        _applyFiltersAndRender();
        return;
    }

    // Búsqueda local inmediata (nombre, teléfono, canal, razón)
    let localFiltered = allContacts.filter(contact => {
        const haystack = [
            contact.display_name || '',
            contact.phone || '',
            contact.canal_origen || '',
            contact.handoff_reason || '',
        ].join(' ').toLowerCase();
        return haystack.includes(term);
    });
    // Aplicar filtros activos sobre el resultado de búsqueda
    if (activePortalFilter) localFiltered = localFiltered.filter(c => c.canal_origen === activePortalFilter);
    if (activeStageFilter)  localFiltered = localFiltered.filter(c => c.current_stage === activeStageFilter);

    // Mostrar resultados locales inmediatamente
    renderContactsList(localFiltered);

    // Si el término es corto (< 3 caracteres), solo hacer búsqueda local
    if (term.length < 3) {
        return;
    }

    // ── Detección de teléfono: si es un número, llamar /hydrate directo ──
    // /contacts/search usa fulltext de MongoDB sobre el body de los mensajes,
    // NO sobre el campo phone. Para búsqueda por número necesitamos el endpoint
    // /hydrate que consulta Redis + HubSpot directamente.
    const phoneE164 = _detectPhoneTerm(term);

    // Debounce 500ms para no spamear requests mientras el usuario escribe
    clearTimeout(searchDebounceTimeout);
    searchDebounceTimeout = setTimeout(async () => {
        // Evitar carrera: si el término cambió durante el debounce, abortar
        if (window._currentSearchTerm !== term) return;

        const localPhones = new Set(localFiltered.map(c => c.phone));
        const allContactPhones = new Set(allContacts.map(c => c.phone));
        let additionalFromServer = [];
        let additionalFromLocal = [];

        // ── Rama A: Búsqueda por teléfono vía /hydrate ──
        if (phoneE164) {
            try {
                const hydrateUrl = `${BASE_URL}/contacts/${encodeURIComponent(phoneE164)}/hydrate`;
                const hResp = await fetch(hydrateUrl, {
                    headers: { 'X-API-Key': API_KEY }
                });
                if (hResp.ok) {
                    const hydrated = await hResp.json();
                    if (hydrated && hydrated.phone) {
                        // Si ya existe en allContacts, solo traerlo a primera plana
                        if (allContactPhones.has(hydrated.phone)) {
                            const existing = allContacts.find(c => c.phone === hydrated.phone);
                            if (existing && !localPhones.has(existing.phone)) {
                                additionalFromLocal.push(existing);
                            }
                        } else {
                            additionalFromServer.push({
                                ...hydrated,
                                _from_search: true,
                                cross_advisor: !!(
                                    ADVISOR_ID && hydrated.owner_id &&
                                    String(hydrated.owner_id) !== String(ADVISOR_ID)
                                ),
                            });
                        }
                    }
                } else if (hResp.status === 404) {
                    console.log(`[Panel] /hydrate 404 para ${phoneE164}`);
                    // No encontrado — dejar que renderContactsList muestre empty state
                }
            } catch (e) {
                console.warn('[Panel] Error consultando /hydrate:', e);
            }
        }

        // ── Rama B: Búsqueda en historial de mensajes (fulltext) ──
        // CRÍTICO: SOLO ejecutar si NO es un teléfono. El fulltext de MongoDB
        // tokeniza por separadores y, al buscar un número, matchea tokens comunes
        // (57, 313, 840, ...) en CUALQUIER mensaje → 12+ falsos positivos.
        // Para búsqueda por teléfono, la rama A (/hydrate) ya dio la respuesta correcta.
        if (!phoneE164) {
            try {
                const response = await fetch(
                    `${BASE_URL}/contacts/search?q=${encodeURIComponent(term)}&limit=20`,
                    { headers: { 'X-API-Key': API_KEY } }
                );
                if (response.ok) {
                    const data = await response.json();
                    const serverPhones = data.phones || [];

                    // Phones de allContacts no incluidos en localFiltered
                    const moreFromLocal = allContacts.filter(contact =>
                        serverPhones.includes(contact.phone) &&
                        !localPhones.has(contact.phone) &&
                        !additionalFromLocal.some(c => c.phone === contact.phone)
                    );
                    additionalFromLocal = [...additionalFromLocal, ...moreFromLocal];

                    const alreadyAddedPhones = new Set([
                        ...additionalFromServer.map(c => c.phone),
                        ...additionalFromLocal.map(c => c.phone),
                    ]);
                    const serverContacts = data.contacts || [];
                    const moreFromServer = serverContacts
                        .filter(c =>
                            !allContactPhones.has(c.phone) &&
                            !localPhones.has(c.phone) &&
                            !alreadyAddedPhones.has(c.phone)
                        )
                        .map(c => ({
                            ...c,
                            _from_search: true,
                            cross_advisor: !!(
                                ADVISOR_ID && c.owner_id && String(c.owner_id) !== String(ADVISOR_ID)
                            ),
                        }));
                    additionalFromServer = [...additionalFromServer, ...moreFromServer];
                }
            } catch (error) {
                console.warn('[Panel] Error en búsqueda de historial:', error);
            }
        }

        // Si el término cambió mientras esperábamos, abortar el render
        if (window._currentSearchTerm !== term) return;

        const allAdditional = [...additionalFromLocal, ...additionalFromServer];
        if (allAdditional.length > 0) {
            console.log(
                `[Panel] Búsqueda: +${additionalFromLocal.length} local, ` +
                `+${additionalFromServer.length} servidor` +
                (phoneE164 ? ` (phone detectado: ${phoneE164})` : '')
            );
            renderContactsList([...localFiltered, ...allAdditional]);
        } else if (localFiltered.length === 0) {
            // Ningún resultado en ningún lado — re-render para que el empty state
            // contextual se muestre correctamente
            renderContactsList([]);
        }
    }, 500);
}

// =========================================================================
// FUNCION DE ACTUALIZACION DE ETAPA DE PIPELINE
// =========================================================================

async function updateDealStage(contactId, stageId) {
    // Localizar el contacto: necesario para el body (auto-cierre) y para revertir
    // el dropdown si el usuario cancela la confirmación de "No responde".
    const contact = allContacts.find(c => String(c.contact_id) === String(contactId));
    const dropdownEl = document.querySelector(`select[data-contact-id="${contactId}"]`);

    // Confirmación específica para "No responde" (stage_id === "other") — evita
    // cierres accidentales por click incorrecto en el dropdown del embudo.
    if (stageId === 'other') {
        const ok = confirm(
            "Marcar como 'No responde'?\n\n" +
            "- El contacto se movera al embudo 'No responde' en HubSpot\n" +
            "- La conversacion se cerrara y desaparecera del panel\n" +
            "- Sofia se reactivara si el cliente vuelve a escribir"
        );
        if (!ok) {
            const previous = contactDealCache[contactId]?.current_stage || '';
            if (dropdownEl && previous) dropdownEl.value = previous;
            return;
        }
    }

    try {
        showLoader();

        const body = { stage_id: stageId };
        if (stageId === 'other' && contact) {
            body.phone = contact.phone;
            body.canal = contact.canal || 'whatsapp';
        }

        const response = await fetch(`${BASE_URL}/contacts/${contactId}/stage`, {
            method: 'PATCH',
            headers: {
                'Content-Type': 'application/json',
                'X-API-Key': API_KEY
            },
            body: JSON.stringify(body)
        });

        const data = await response.json();

        if (response.ok) {
            const stageName = PIPELINE_STAGES.find(s => s.id === stageId)?.name || stageId;
            console.log(`[Panel] Etapa actualizada: ${stageName}`);

            if (contactId) {
                contactDealCache[contactId] = { current_stage: stageId };
            }

            const contactEl = dropdownEl?.closest('.contact-item[data-phone]');
            if (contactEl) {
                const phone = contactEl.dataset.phone;
                const currentFP = _contactFingerprints.get(phone);
                if (currentFP) {
                    const parts = currentFP.split('|');
                    parts[4] = stageId;
                    _contactFingerprints.set(phone, parts.join('|'));
                }
            }

            if (dropdownEl) {
                dropdownEl.classList.add('ring-2', 'ring-yellow-400');
                setTimeout(() => {
                    dropdownEl.classList.remove('ring-2', 'ring-yellow-400');
                }, 1500);
            }

            // Auto-cierre: si el backend cerró la conversación, limpiar UI igual
            // que el botón "Cerrar". Se basa en data.closed devuelto por el endpoint.
            if (data.closed && body.phone) {
                _performCloseCleanup(body.phone);
            } else if (stageId === 'other' && data.close_error) {
                console.warn('[Panel] Auto-cierre falló:', data.close_error);
                alert("Etapa actualizada, pero el cierre automatico fallo. Cierra manualmente con el boton 'Cerrar'.");
            }
        } else {
            throw new Error(data.detail || 'Error actualizando etapa');
        }
    } catch (error) {
        console.error('[Panel] Error actualizando etapa:', error);
        alert('Error al actualizar etapa: ' + error.message);
        loadContacts();
    } finally {
        hideLoader();
    }
}

// =========================================================================
// FUNCIONES DE TEMPLATES
// =========================================================================

async function loadTemplates(forceRefresh = false) {
    // Usar caché de sesión: los templates no cambian durante una sesión normal
    if (!forceRefresh && templatesData && templatesData.length > 0) {
        populateTemplateSelector();
        return;
    }
    showLoader();
    try {
        const response = await fetch(`${BASE_URL}/templates?advisor_id=${encodeURIComponent(ADVISOR_ID)}`, {
            headers: { 'X-API-Key': API_KEY }
        });

        if (!response.ok) throw new Error('Error al cargar templates');

        const data = await response.json();
        templatesData = data.templates || [];
        populateTemplateSelector();

    } catch (error) {
        console.error('[Panel] Error cargando templates:', error);
    } finally {
        hideLoader();
    }
}

// =========================================================================
// TEMPLATE PICKER (slash command)
// =========================================================================

function populateTemplateSelector() {
    // Compatibilidad: no hace nada si el selector antiguo ya no existe
    // Los templates se cargan via renderTemplatePicker() ahora
}

function openTemplatePicker() {
    if (!currentContactId) return;
    const picker = document.getElementById('templatePicker');
    if (!picker) return;
    picker.classList.remove('hidden');
    renderTemplatePicker('');
    const search = document.getElementById('templateSearch');
    if (search) search.value = '';
}

function closeTemplatePicker() {
    const picker = document.getElementById('templatePicker');
    if (picker) picker.classList.add('hidden');
    pickerSelectedIndex = -1;
    pickerVisibleItems = [];
    // Si el input solo tiene "/" lo limpiamos
    const inp = document.getElementById('messageInput');
    if (inp && inp.value === '/') inp.value = '';
    if (!currentWindowOpen) {
        if (!activeTemplateId) {
            // Asesor canceló sin elegir template: re-deshabilitar input
            if (inp) {
                inp.disabled = true;
                inp.placeholder = 'Ventana cerrada. Usa un template para reactivar.';
                inp.classList.add('bg-gray-200', 'cursor-not-allowed');
            }
        } else {
            // Template seleccionado con ventana cerrada: habilitar input y sendBtn
            if (inp) {
                inp.disabled = false;
                inp.classList.remove('bg-gray-200', 'cursor-not-allowed');
            }
            const sendBtn = document.getElementById('sendBtn');
            if (sendBtn) sendBtn.disabled = false;
        }
    }
}

function renderTemplatePicker(filter) {
    const list = document.getElementById('templateList');
    if (!list) return;

    const f = (filter || '').toLowerCase();
    const categoryIcons = {
        cita: '📅', reactivacion: '📨', seguimiento: '🔄',
        recordatorio: '⏰', promocion: '🎯', agradecimiento: '🙏', otros: '📝'
    };

    const categories = {};
    templatesData
        .filter(t => !f
            || t.name.toLowerCase().includes(f)
            || (t.category || '').toLowerCase().includes(f)
            || (t.id || '').toLowerCase().includes(f))
        .forEach(t => {
            const cat = t.category || 'otros';
            if (!categories[cat]) categories[cat] = [];
            categories[cat].push(t);
        });

    pickerVisibleItems = [];
    let html = '';
    let idx = 0;

    if (Object.keys(categories).length === 0) {
        html = '<p class="text-sm text-gray-400 text-center py-5">Sin resultados para "' + filter + '"</p>';
        list.innerHTML = html;
        pickerSelectedIndex = -1;
        return;
    }

    Object.keys(categories).sort().forEach(cat => {
        const icon = categoryIcons[cat] || '📝';
        const catLabel = cat.charAt(0).toUpperCase() + cat.slice(1);
        html += `<div class="px-4 py-1.5 bg-gray-50 border-b border-gray-100 sticky top-0">
            <span class="text-xs font-semibold text-gray-400 uppercase tracking-wide">${icon} ${catLabel}</span>
        </div>`;
        categories[cat].forEach(t => {
            const preview = (t.body || '').replace(/\n/g, ' ').substring(0, 90);
            const escapedId = t.id.replace(/'/g, "\\'");
            html += `<div class="template-picker-item flex items-baseline gap-2 px-4 py-2.5 cursor-pointer border-b border-gray-50 hover:bg-blue-50 transition-colors"
                         data-idx="${idx}" onclick="selectTemplate('${escapedId}')">
                <span class="font-semibold text-gray-800 text-sm whitespace-nowrap shrink-0">/${t.name}</span>
                <span class="text-gray-500 text-sm truncate">${preview}</span>
            </div>`;
            pickerVisibleItems.push(t.id);
            idx++;
        });
    });

    list.innerHTML = html;
    pickerSelectedIndex = -1;
}

function selectTemplate(templateId) {
    const template = templatesData.find(t => t.id === templateId);
    if (!template) return;

    // Asignar estado ANTES de cerrar el picker para que closeTemplatePicker
    // vea activeTemplateId ya establecido (evita re-deshabilitar el input)
    activeTemplateId = templateId;
    activeTemplateBody = template.body;
    activeTemplateVars = template.variables || [];

    closeTemplatePicker();

    const inp = document.getElementById('messageInput');
    if (!inp) return;
    inp.value = template.body;
    inp.focus();

    // Auto-seleccionar primer {variable}
    jumpToNextVariable(inp, 1, true);

    // Mostrar hint si hay variables
    const hint = document.getElementById('templateHint');
    if (hint) {
        if (activeTemplateVars.length > 0) {
            hint.classList.remove('hidden');
        } else {
            hint.classList.add('hidden');
        }
    }
}

function jumpToNextVariable(textarea, direction, fromStart) {
    const text = textarea.value;
    const cursor = fromStart ? 0 : textarea.selectionEnd;
    const regex = /\{(\w+)\}/g;
    let match;
    const matches = [];
    while ((match = regex.exec(text)) !== null) {
        matches.push({ start: match.index, end: match.index + match[0].length });
    }
    if (matches.length === 0) {
        const hint = document.getElementById('templateHint');
        if (hint) hint.classList.add('hidden');
        return false;
    }
    let target;
    if (direction === 1) {
        target = matches.find(m => m.start >= (fromStart ? 0 : cursor)) || matches[0];
    } else {
        const before = matches.filter(m => m.end <= cursor - 1);
        target = before.length > 0 ? before[before.length - 1] : matches[matches.length - 1];
    }
    textarea.setSelectionRange(target.start, target.end);
    return true;
}

function extractVariableValues(originalBody, editedText, variables) {
    if (!variables || variables.length === 0) return {};

    // Normalizar: trim + colapsar whitespace múltiple (tolera espacios/newlines extra)
    const normalizedEdit = editedText.trim().replace(/\s+/g, ' ');
    const normalizedBody = originalBody.trim().replace(/\s+/g, ' ');

    // Escapar caracteres especiales de regex en el template original
    let pattern = normalizedBody.replace(/[-[\]/{}()*+?.\\^$|]/g, '\\$&');
    // Reemplazar cada \{varname\} con un grupo de captura
    variables.forEach(v => {
        pattern = pattern.replace(`\\{${v}\\}`, '([\\s\\S]+?)');
    });
    // El último grupo debe ser greedy para capturar hasta el final
    pattern = pattern.replace(/\(\[\\s\\S\]\+\?\)(?=[^(]*$)/, '([\\s\\S]+)');
    try {
        // Intentar match exacto primero
        let match = normalizedEdit.match(new RegExp('^' + pattern + '$'));
        if (!match) {
            // Fallback: permitir texto extra al inicio/final (asesora agregó algo)
            match = normalizedEdit.match(new RegExp(pattern));
        }
        if (!match) return {};
        const result = {};
        variables.forEach((v, i) => { result[v] = (match[i + 1] || '').trim(); });
        return result;
    } catch (e) {
        return {};
    }
}

function movePickerSelection(direction) {
    if (pickerVisibleItems.length === 0) return;
    const items = document.querySelectorAll('.template-picker-item');
    items.forEach(i => i.classList.remove('bg-blue-100'));
    pickerSelectedIndex = Math.max(0, Math.min(pickerVisibleItems.length - 1, pickerSelectedIndex + direction));
    const selected = items[pickerSelectedIndex];
    if (selected) {
        selected.classList.add('bg-blue-100');
        selected.scrollIntoView({ block: 'nearest' });
    }
}

function confirmPickerSelection() {
    if (pickerSelectedIndex >= 0 && pickerVisibleItems[pickerSelectedIndex]) {
        selectTemplate(pickerVisibleItems[pickerSelectedIndex]);
    } else if (pickerVisibleItems.length > 0) {
        // Si no hay selección por teclado, seleccionar el primero
        selectTemplate(pickerVisibleItems[0]);
    }
}

function _initTemplatePickerListeners() {
    const inp = document.getElementById('messageInput');
    if (!inp || inp._templateListenerAdded) return;
    inp._templateListenerAdded = true;

    // Detectar "/" para abrir picker
    inp.addEventListener('input', function() {
        const val = this.value;
        const pickerEl = document.getElementById('templatePicker');
        const pickerOpen = pickerEl && !pickerEl.classList.contains('hidden');

        if (val === '/') {
            // Abrir picker al escribir solo "/"
            if (templatesData.length === 0) {
                loadTemplates().then(() => openTemplatePicker());
            } else {
                openTemplatePicker();
            }
        } else if (val.startsWith('/') && pickerOpen) {
            // Filtrar mientras se escribe despues del slash
            const filter = val.slice(1);
            const search = document.getElementById('templateSearch');
            if (search) search.value = filter;
            renderTemplatePicker(filter);
        } else if (!val.startsWith('/') && pickerOpen) {
            // Borro el slash → cerrar picker
            const pickerEl2 = document.getElementById('templatePicker');
            if (pickerEl2) pickerEl2.classList.add('hidden');
            pickerSelectedIndex = -1;
            pickerVisibleItems = [];
        }

        // Si el usuario edita el template activo y ya no quedan {variables}
        if (activeTemplateId && !/\{\w+\}/.test(val)) {
            const hint = document.getElementById('templateHint');
            if (hint) hint.classList.add('hidden');
        }
    });

    // Teclado: navegación en picker + Tab entre variables
    inp.addEventListener('keydown', function(e) {
        const pickerEl = document.getElementById('templatePicker');
        const pickerOpen = pickerEl && !pickerEl.classList.contains('hidden');

        if (pickerOpen) {
            if (e.key === 'ArrowDown')  { e.preventDefault(); movePickerSelection(1); return; }
            if (e.key === 'ArrowUp')    { e.preventDefault(); movePickerSelection(-1); return; }
            if (e.key === 'Enter')      { e.preventDefault(); confirmPickerSelection(); return; }
            if (e.key === 'Escape')     { e.preventDefault(); closeTemplatePicker(); return; }
            // Mientras el picker está abierto no enviamos con Enter
            return;
        }

        // Tab para saltar entre {variables} del template activo
        if (e.key === 'Tab' && activeTemplateId) {
            e.preventDefault();
            jumpToNextVariable(this, e.shiftKey ? -1 : 1, false);
        }
    });

    // Buscador interno del picker sincroniza con el input principal
    const search = document.getElementById('templateSearch');
    if (search) {
        search.addEventListener('input', function() {
            renderTemplatePicker(this.value);
            // Sincronizar con messageInput
            inp.value = '/' + this.value;
        });
        search.addEventListener('keydown', function(e) {
            if (e.key === 'ArrowDown') { e.preventDefault(); inp.focus(); movePickerSelection(1); }
            if (e.key === 'ArrowUp')   { e.preventDefault(); inp.focus(); movePickerSelection(-1); }
            if (e.key === 'Escape')    { e.preventDefault(); closeTemplatePicker(); inp.focus(); }
            if (e.key === 'Enter')     { e.preventDefault(); confirmPickerSelection(); }
        });
    }

    // Botón "/" para ventana cerrada: habilita input temporalmente y abre picker
    const triggerBtn = document.getElementById('templateTriggerBtn');
    if (triggerBtn && !triggerBtn._listenerAdded) {
        triggerBtn._listenerAdded = true;
        triggerBtn.addEventListener('click', () => {
            // Habilitar input temporalmente para que el asesor pueda editar variables
            if (inp) {
                inp.disabled = false;
                inp.placeholder = 'Edita los campos del template y presiona Enviar';
                inp.classList.remove('bg-gray-200', 'cursor-not-allowed');
            }
            if (templatesData.length === 0) {
                loadTemplates().then(() => openTemplatePicker());
            } else {
                openTemplatePicker();
            }
        });
    }
}

async function openTemplateModal() {
    // Crear modal si no existe
    let modal = document.getElementById('templateModal');
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'templateModal';
        modal.className = 'fixed inset-0 bg-black bg-opacity-50 z-50 flex items-center justify-center hidden';
        modal.innerHTML = `
            <div class="bg-white rounded-lg shadow-xl w-full max-w-2xl max-h-[90vh] overflow-hidden">
                <div class="p-4 border-b flex justify-between items-center bg-gray-50">
                    <h2 class="text-lg font-semibold">Administrar Templates</h2>
                    <button onclick="closeTemplateModal()" class="text-gray-500 hover:text-gray-700 text-xl">&times;</button>
                </div>
                <div class="p-4 overflow-y-auto max-h-[60vh]" id="templateList">
                    <p class="text-gray-500">Cargando templates...</p>
                </div>
                <div class="p-4 border-t bg-gray-50">
                    <button onclick="showCreateTemplateForm()" class="bg-[#F5C400] text-[#1A1A1A] font-semibold px-4 py-2 rounded hover:bg-[#D4A800]">
                        + Crear Template
                    </button>
                </div>
            </div>
        `;
        document.body.appendChild(modal);
    }

    // Cargar y mostrar templates
    await loadTemplates();
    renderTemplateList();

    modal.classList.remove('hidden');
}

function closeTemplateModal() {
    const modal = document.getElementById('templateModal');
    if (modal) modal.classList.add('hidden');
}

function renderTemplateList() {
    const container = document.getElementById('templateList');
    if (!container) return;

    if (templatesData.length === 0) {
        container.innerHTML = '<p class="text-gray-500 text-center py-4">No hay templates</p>';
        return;
    }

    const categoryIcons = {
        'reactivacion': '&#128236;',
        'cita': '&#128197;',
        'seguimiento': '&#128260;',
        'recordatorio': '&#9200;',
        'promocion': '&#127919;',
        'otros': '&#128221;'
    };

    container.innerHTML = templatesData.map(t => `
        <div class="border rounded-lg p-3 mb-2 ${t.is_default ? 'bg-blue-50' : 'bg-white'}">
            <div class="flex justify-between items-start">
                <div class="flex-1">
                    <div class="flex items-center gap-2">
                        <span>${categoryIcons[t.category] || '&#128221;'}</span>
                        <span class="font-medium">${t.name}</span>
                        ${t.is_default ? '<span class="text-xs bg-blue-100 text-blue-700 px-2 py-0.5 rounded">Predefinido</span>' : ''}
                    </div>
                    <p class="text-sm text-gray-600 mt-1">${t.body.substring(0, 100)}${t.body.length > 100 ? '...' : ''}</p>
                    ${t.variables && t.variables.length > 0 ?
            `<p class="text-xs text-gray-400 mt-1">Variables: ${t.variables.join(', ')}</p>` : ''}
                </div>
                <div class="flex gap-1">
                    <button onclick="editTemplate('${t.id}')" class="text-blue-500 hover:text-blue-700 p-1" title="Editar">&#9999;&#65039;</button>
                    ${!t.is_default ?
            `<button onclick="deleteTemplate('${t.id}')" class="text-red-500 hover:text-red-700 p-1" title="Eliminar">&#128465;&#65039;</button>` : ''}
                </div>
            </div>
        </div>
    `).join('');
}

function showCreateTemplateForm() {
    const container = document.getElementById('templateList');
    container.innerHTML = `
        <form id="templateForm" onsubmit="saveTemplate(event)">
            <div class="mb-3">
                <label class="block text-sm font-medium mb-1">Nombre</label>
                <input type="text" name="name" required
                    class="w-full border rounded px-3 py-2"
                    placeholder="Ej: Seguimiento post-visita">
            </div>
            <div class="mb-3">
                <label class="block text-sm font-medium mb-1">Categoria</label>
                <select name="category" required class="w-full border rounded px-3 py-2">
                    <option value="reactivacion">&#128236; Reactivacion</option>
                    <option value="cita">&#128197; Cita</option>
                    <option value="seguimiento">&#128260; Seguimiento</option>
                    <option value="recordatorio">&#9200; Recordatorio</option>
                    <option value="promocion">&#127919; Promocion</option>
                </select>
            </div>
            <div class="mb-3">
                <label class="block text-sm font-medium mb-1">Mensaje</label>
                <textarea name="body" required rows="4"
                    class="w-full border rounded px-3 py-2"
                    placeholder="Usa {nombre}, {fecha}, {hora} para variables"></textarea>
                <p class="text-xs text-gray-500 mt-1">Variables disponibles: {nombre}, {fecha}, {hora}, {direccion}</p>
            </div>
            <div class="flex gap-2">
                <button type="submit" class="bg-[#F5C400] text-[#1A1A1A] font-semibold px-4 py-2 rounded hover:bg-[#D4A800]">
                    Guardar
                </button>
                <button type="button" onclick="renderTemplateList()" class="bg-gray-200 px-4 py-2 rounded hover:bg-gray-300">
                    Cancelar
                </button>
            </div>
        </form>
    `;
}

async function saveTemplate(event) {
    event.preventDefault();
    const form = event.target;
    const formData = new FormData(form);

    // Relajar validación: permitir cualquier nombre, solo bloquear si está vacío
    const name = formData.get('name').trim();
    if (!name) {
        alert('El nombre no puede estar vacío.');
        return;
    }

    // Detectar variables en el body
    const body = formData.get('body');
    const variables = [];
    const regex = /\{(\w+)\}/g;
    let match;
    while ((match = regex.exec(body)) !== null) {
        if (!variables.includes(match[1])) {
            variables.push(match[1]);
        }
    }
    // Siempre enviar variables como JSON válido
    formData.append('variables', JSON.stringify(variables));

    try {
        const response = await fetch(`${BASE_URL}/templates?advisor_id=${encodeURIComponent(ADVISOR_ID)}`, {
            method: 'POST',
            headers: { 'X-API-Key': API_KEY },
            body: formData
        });

        let data = null;
        try {
            data = await response.json();
        } catch (e) {
            // Si no es JSON, mostrar mensaje genérico
            alert('Error interno del servidor. Intenta nuevamente o contacta soporte.');
            return;
        }

        if (response.ok) {
            alert('Template creado exitosamente');
            await loadTemplates(true);
            renderTemplateList();
        } else {
            // Mensajes claros según código
            if (response.status === 409) {
                alert('Ya existe un template con ese nombre. Elige otro nombre.');
            } else if (response.status === 400) {
                alert('Error en los datos enviados. Revisa los campos y variables.');
            } else if (response.status === 500) {
                alert('Error interno al guardar la plantilla. Intenta nuevamente o contacta soporte.');
            } else {
                alert((data && data.detail) || 'Error creando template');
            }
        }
    } catch (error) {
        alert('Error: ' + error.message);
    }
}

async function editTemplate(templateId) {
    const template = templatesData.find(t => t.id === templateId);
    if (!template) return;

    const container = document.getElementById('templateList');
    container.innerHTML = `
        <form id="templateForm" onsubmit="updateTemplate(event, '${templateId}')">
            <div class="mb-3">
                <label class="block text-sm font-medium mb-1">Nombre</label>
                <input type="text" name="name" required value="${template.name}"
                    class="w-full border rounded px-3 py-2">
            </div>
            <div class="mb-3">
                <label class="block text-sm font-medium mb-1">Categoria</label>
                <select name="category" required class="w-full border rounded px-3 py-2">
                    <option value="reactivacion" ${template.category === 'reactivacion' ? 'selected' : ''}>&#128236; Reactivacion</option>
                    <option value="cita" ${template.category === 'cita' ? 'selected' : ''}>&#128197; Cita</option>
                    <option value="seguimiento" ${template.category === 'seguimiento' ? 'selected' : ''}>&#128260; Seguimiento</option>
                    <option value="recordatorio" ${template.category === 'recordatorio' ? 'selected' : ''}>&#9200; Recordatorio</option>
                    <option value="promocion" ${template.category === 'promocion' ? 'selected' : ''}>&#127919; Promocion</option>
                </select>
            </div>
            <div class="mb-3">
                <label class="block text-sm font-medium mb-1">Mensaje</label>
                <textarea name="body" required rows="4"
                    class="w-full border rounded px-3 py-2">${template.body}</textarea>
            </div>
            <div class="flex gap-2">
                <button type="submit" class="bg-blue-500 text-white px-4 py-2 rounded hover:bg-blue-600">
                    Actualizar
                </button>
                <button type="button" onclick="renderTemplateList()" class="bg-gray-200 px-4 py-2 rounded hover:bg-gray-300">
                    Cancelar
                </button>
            </div>
        </form>
    `;
}

async function updateTemplate(event, templateId) {
    event.preventDefault();
    const form = event.target;
    const formData = new FormData(form);

    // Detectar variables
    const body = formData.get('body');
    const variables = [];
    const regex = /\{(\w+)\}/g;
    let match;
    while ((match = regex.exec(body)) !== null) {
        if (!variables.includes(match[1])) {
            variables.push(match[1]);
        }
    }
    formData.append('variables', JSON.stringify(variables));

    try {
        const response = await fetch(`${BASE_URL}/templates/${templateId}?advisor_id=${encodeURIComponent(ADVISOR_ID)}`, {
            method: 'PUT',
            headers: { 'X-API-Key': API_KEY },
            body: formData
        });

        const data = await response.json();

        if (response.ok) {
            alert('Template actualizado');
            await loadTemplates(true);
            renderTemplateList();
        } else {
            throw new Error(data.detail || 'Error actualizando template');
        }
    } catch (error) {
        alert('Error: ' + error.message);
    }
}

async function deleteTemplate(templateId) {
    if (!confirm('Eliminar este template?')) return;

    try {
        const response = await fetch(`${BASE_URL}/templates/${templateId}?advisor_id=${encodeURIComponent(ADVISOR_ID)}`, {
            method: 'DELETE',
            headers: { 'X-API-Key': API_KEY }
        });

        const data = await response.json();

        if (response.ok) {
            alert('Template eliminado');
            await loadTemplates(true);
            renderTemplateList();
        } else {
            throw new Error(data.detail || 'Error eliminando template');
        }
    } catch (error) {
        alert('Error: ' + error.message);
    }
}

// =========================================================================
// FUNCIONES DE CARGA DE DATOS
// =========================================================================

// Variable global para el worker filter activo
let activeWorkerFilter = '';
let activeWorkerName = '';  // nombre legible del worker activo (para empty state)

// Variable global para el portal filter activo (filtrado local en allContacts)
let activePortalFilter = '';

// Variable global para el filtro de etapa activo
let activeStageFilter = '';

// Mapa de canal_origen → label corto para los chips de portal
const CANAL_LABELS = {
    finca_raiz: 'FR', metrocuadrado: 'MC', instagram: 'IG',
    facebook: 'FB', whatsapp_directo: 'WA', pagina_web: 'WEB',
    tiktok: 'TT', ciencuadras: 'CC', charly: 'CH'
};

/**
 * Aplica filtros activos (portal + etapa) sobre allContacts y renderiza.
 * Llamado desde loadContacts(), filterByPortal(), onStageFilterChange().
 */
function _applyFiltersAndRender() {
    // CRÍTICO: si hay búsqueda activa, delegar a filterContacts para no clobberear
    // los resultados de búsqueda con el polling periódico.
    const _activeTerm = (window._currentSearchTerm || '').trim();
    if (_activeTerm) {
        // Re-ejecuta filterContacts que hará render local + eventualmente /hydrate
        filterContacts(_activeTerm);
        return;
    }
    let filtered = allContacts;
    if (activePortalFilter) filtered = filtered.filter(c => c.canal_origen === activePortalFilter);
    if (activeStageFilter)  filtered = filtered.filter(c => c.current_stage === activeStageFilter);
    renderContactsList(filtered);
}

/**
 * Reconstruye los chips de portal según los canales realmente presentes en contacts.
 * @param {Array} contacts - Array de contactos (allContacts)
 */
function _rebuildPortalChips(contacts) {
    const row = document.getElementById('portalFilterRow');
    if (!row) return;
    const todosBtn = row.querySelector('[data-canal=""]');
    const canales = [...new Set(contacts.map(c => c.canal_origen).filter(Boolean))].sort();
    row.innerHTML = '';
    if (todosBtn) row.appendChild(todosBtn);
    canales.forEach(canal => {
        const label = CANAL_LABELS[canal] || canal.slice(0, 3).toUpperCase();
        const btn = document.createElement('button');
        btn.className = `portal-chip${activePortalFilter === canal ? ' active' : ''}`;
        btn.dataset.canal = canal;
        btn.textContent = label;
        btn.onclick = () => filterByPortal(canal);
        row.appendChild(btn);
    });
    // Si el filtro activo ya no existe en los canales, resetear a "Todos"
    if (activePortalFilter && !canales.includes(activePortalFilter)) {
        activePortalFilter = '';
        if (todosBtn) todosBtn.classList.add('active');
    }
}

/**
 * Handler del select de filtro por etapa.
 * @param {string} val - ID de la etapa, o '' para todas
 */
function onStageFilterChange(val) {
    activeStageFilter = val;
    // [StageFilter] Backend hace el filtro de etapa (trae todos del owner en esa etapa
    // desde HubSpot, cap 500). Sin etapa, loadContacts() vuelve al flujo ZSET normal.
    loadContacts();
    _updateBulkButtonVisibility();
}

/**
 * Pobla el select #stageFilter con las etapas del pipeline.
 * Llamado una sola vez en la inicialización.
 */
function _initStageFilter() {
    const sel = document.getElementById('stageFilter');
    if (!sel) return;
    PIPELINE_STAGES.forEach(s => {
        const opt = document.createElement('option');
        opt.value = s.id;
        opt.textContent = s.name;
        sel.appendChild(opt);
    });
}

/**
 * Activa o desactiva el filtro de portal y re-renderiza la lista.
 * @param {string} canal - Valor de canal_origen, o '' para mostrar todos
 */
function filterByPortal(canal) {
    activePortalFilter = canal;
    // Actualizar UI de chips
    document.querySelectorAll('#portalFilterRow .portal-chip').forEach(btn => {
        if (btn.dataset.canal === canal) {
            btn.classList.add('active');
        } else {
            btn.classList.remove('active');
        }
    });
    _applyFiltersAndRender();
}

async function loadWorkerFilterOptions() {
    const sel = document.getElementById('workerFilter');
    if (!sel) return;
    try {
        const resp = await fetch(`${BASE_URL}/workers`, { headers: { 'X-API-Key': API_KEY } });
        if (!resp.ok) return;
        const data = await resp.json();
        const workers = data.workers || [];
        // Conservar la opción "Todas las conversaciones" y agregar workers
        sel.innerHTML = '<option value="">&#128197; Todas las conversaciones</option>';
        workers.forEach(w => {
            const opt = document.createElement('option');
            opt.value = w._id || w.id || w.worker_id || w.name;
            opt.textContent = `👤 Citas de ${w.name}`;
            sel.appendChild(opt);
        });
        // Restaurar selección activa si había una
        if (activeWorkerFilter) sel.value = activeWorkerFilter;
    } catch (err) {
        console.warn('[Panel] No se pudieron cargar workers para filtro:', err);
    }
}

function onWorkerFilterChange(workerId) {
    activeWorkerFilter = workerId;
    // Guardar nombre legible del worker para el empty state
    const sel = document.getElementById('workerFilter');
    const selectedOpt = sel ? sel.options[sel.selectedIndex] : null;
    activeWorkerName = selectedOpt && workerId
        ? selectedOpt.textContent.replace('👤 Citas de', '').trim()
        : '';
    loadContacts();
}

async function loadContacts() {
    const workerSel = document.getElementById('workerFilter');
    const workerIdParam = workerSel ? workerSel.value : '';

    // Un solo input de fecha — filtra por un día específico
    const dateFrom = document.getElementById('dateFrom')?.value;

    // Construir URL base según modo activo
    let url;
    if (workerIdParam) {
        url = `${BASE_URL}/contacts?worker_id=${encodeURIComponent(workerIdParam)}`;
    } else if (dateFrom) {
        url = `${BASE_URL}/contacts?filter_time=custom`;
    } else {
        // Sin fecha: mostrar todos los contactos activos sin filtro histórico
        url = `${BASE_URL}/contacts?filter_time=all`;
    }

    // Agregar filtro por advisor si esta presente en la URL
    if (ADVISOR_ID) {
        url += `&advisor=${ADVISOR_ID}`;
    }

    // [Sync] Primera carga con deep link: incluir teléfono aunque sea cross-advisor
    if (!deepLinkHandled) {
        const _dlPhone = urlParams.get('phone');
        if (_dlPhone) {
            let _dlNorm = _dlPhone.replace(/\s+/g, '').trim();
            if (_dlNorm && !_dlNorm.startsWith('+') && /^\d/.test(_dlNorm)) {
                _dlNorm = '+' + _dlNorm;
            }
            if (_dlNorm) url += `&include_phone=${encodeURIComponent(_dlNorm)}`;
        }
    }

    // Agregar fecha: si dateFrom tiene valor, se filtra ese día completo (00:00 → 23:59)
    if (dateFrom) {
        url += `&date_from=${dateFrom}T00:00:00`;
        url += `&date_to=${dateFrom}T23:59:59`;
    }

    if (!workerIdParam && dateFrom) {
        const dateFieldEl = document.querySelector('input[name="dateField"]:checked');
        const dateField = dateFieldEl ? dateFieldEl.value : 'last_activity';
        url += `&date_field=${dateField}`;
        console.log(`[Filtro] Fecha: ${dateFrom}  |  campo: ${dateField}`);
    } else if (workerIdParam) {
        console.log(`[Filtro] Modo worker: ${workerIdParam} | Fecha: ${dateFrom || '(todas)'}`);
    } else {
        console.log(`[Filtro] Sin fecha — mostrando todos los contactos activos`);
    }

    // [StageFilter] Filtro por etapa: backend trae TODOS los contactos del owner en esa etapa
    if (activeStageFilter) {
        url += `&stage=${encodeURIComponent(activeStageFilter)}`;
        console.log(`[StageFilter] Solicitando todos los contactos en etapa ${activeStageFilter}`);
    }
    console.log(`[Filtro] GET ${url}`);

    try {
        const response = await fetch(url, {
            headers: { 'X-API-Key': API_KEY }
        });

        if (!response.ok) throw new Error('Error al cargar contactos');

        const data = await response.json();
        const newContacts = data.contacts || [];

        // [StageFilter] Aviso si HubSpot devolvió el tope duro (más contactos disponibles allá)
        if (data.filter_mode === 'stage_full' && data.max_reached) {
            try { showToast(`Mostrando los primeros ${data.total} contactos. Hay más en HubSpot.`, 'info'); } catch (_e) {}
        }

        console.log(`[Filtro] Respuesta: ${newContacts.length} contactos  |  activos: ${data.active_count ?? '?'}  |  históricos: ${data.historical_count ?? '?'}  |  rango: ${data.since ?? '?'} → ${data.until ?? '?'}  |  mode: ${data.filter_mode ?? 'normal'}${data.from_cache ? ' (cache)' : ''}`);
        if (newContacts.length === 0) {
            console.warn('[Filtro] ⚠️  El backend devolvió 0 contactos. Causas posibles: rango de fechas sin actividad, filtro de asesora muy restrictivo, o error Redis transitorio.');
        }

        // Fix: guard contra error Redis transitorio — si el backend devuelve 0 contactos
        // pero teníamos una lista previa, es casi seguro un error de pool exhausto (Too many
        // connections). Mantener la lista anterior evita el blink total de la UI.
        // Excepción: si hay un filtro activo (worker o fecha), el 0 es intencional → limpiar lista.
        if (newContacts.length === 0 && allContacts.length > 0 && !workerIdParam && !dateFrom) {
            console.warn('[Filtro] Manteniendo lista anterior para evitar blink vacío (posible error transitorio).');
            return;
        }

        allContacts = newContacts;  // Guardar en cache para el buscador

        // ── Badges persistentes desde servidor (advisor_inbox) ────────────────
        // Inicializa badges para contactos con actividad no leída desde la última sesión.
        // Guards: solo con ADVISOR_ID, solo si el phone no fue visto en esta sesión,
        // solo si el contacto no está actualmente abierto.
        if (ADVISOR_ID) {
            for (const contact of newContacts) {
                const phone = contact.phone || '';
                if (
                    contact.has_unread &&
                    phone &&
                    phone !== currentPhone &&
                    !_seenPhones.has(phone)
                ) {
                    // Solo inicializar si no hay conteo activo mayor (WS puede haber incrementado más)
                    if (!(phone in unreadCounts) || unreadCounts[phone] === 0) {
                        unreadCounts[phone] = 1;
                        console.log('[Panel][Inbox] Badge inicializado desde servidor para', phone);
                    }
                }
            }
        }

        // Reconciliación: si servidor dice has_unread=false pero tenemos badge local → limpiar.
        // El servidor es fuente de verdad para LIMPIAR (advisor marcó como leído en otra tab/sesión).
        // Solo aplica si el contacto no está abierto actualmente ni fue cerrado recientemente.
        for (const contact of newContacts) {
            const _rp = contact.phone || '';
            if (!contact.has_unread && _rp && _rp !== currentPhone &&
                unreadCounts[_rp] > 0 && !recentlyClosedPhones.has(_rp)) {
                console.log('[Panel][Inbox] Servidor limpia badge para', _rp, '(has_unread=false)');
                unreadCounts[_rp] = 0;
                updateUnreadBadge(_rp, 0);
            }
        }

        // Detección de mensajes nuevos por polling.
        // Solo actualiza el timestamp si es mayor (upgrade-only, nunca downgrade).
        // Esto evita falsos positivos cuando el backend devuelve el mismo contacto
        // duplicado con timestamps distintos (la entrada menor sobreescribiría la mayor,
        // causando que el próximo poll detecte un "nuevo mensaje" eternamente).
        for (const contact of allContacts) {
            const phone = contact.phone || '';
            const newTime = contact.last_activity || '';
            const oldTime = _lastContactTimestamps[phone] || '';
            if (newTime > oldTime) {
                const isNewMessage = oldTime !== '';  // no notificar en la carga inicial
                _lastContactTimestamps[phone] = newTime;
                if (isNewMessage) {
                    console.log('[Panel] Nuevo mensaje detectado (polling):', phone);
                    if (phone !== currentPhone) {
                        // Fix CR-4: no incrementar si el WS ya notificó este mensaje en los últimos 10s.
                        // Ventana de 10s cubre servidores lentos (Railway bajo carga puede demorar 6-8s).
                        const wsHandledRecently = (_lastWsNotifiedTimestamps[phone] || 0) > Date.now() - 10000;
                        if (!wsHandledRecently) {
                            unreadCounts[phone] = (unreadCounts[phone] || 0) + 1;
                        }
                        // Siempre actualizar el badge con el contador actual (evita desfase visual)
                        updateUnreadBadge(phone, unreadCounts[phone] || 0);
                    }
                }
            }
        }

        // Filtrar contactos recién cerrados antes de renderizar (guard contra polling stale)
        if (recentlyClosedPhones.size > 0) {
            allContacts = allContacts.filter(c => !recentlyClosedPhones.has(c.phone));
        }

        _rebuildPortalChips(allContacts);
        _applyFiltersAndRender();
        console.log(`[Filtro] Renderizando ${allContacts.length} contactos en lista.`);
        checkSilentBotContacts();

        // Auto-select por deep link (?phone=): ejecutar solo una vez tras la primera carga
        if (!deepLinkHandled) {
            let deepLinkPhone = urlParams.get('phone');
            if (deepLinkPhone) {
                deepLinkHandled = true;
                // Normalizar: quitar espacios y agregar + si no lo tiene
                // (URLs convierten + en espacio, así que "+" llega como " ")
                deepLinkPhone = deepLinkPhone.replace(/\s+/g, '').trim();
                if (deepLinkPhone && !deepLinkPhone.startsWith('+') && /^\d/.test(deepLinkPhone)) {
                    deepLinkPhone = '+' + deepLinkPhone;
                }
                const target = allContacts.find(c => c.phone === deepLinkPhone);
                if (target) {
                    console.log('[Panel] Deep link: auto-seleccionando', deepLinkPhone);
                    selectContact(
                        target.contact_id || '',
                        target.phone,
                        target.display_name || target.phone,
                        target.canal || target.canal_origen || 'whatsapp'
                    );
                } else {
                    // Contacto no está en la lista tras include_phone → hidratar vía /hydrate
                    _handleDeepLinkMiss(deepLinkPhone);
                }
            }
        }


    } catch (error) {
        console.error('Error cargando contactos:', error);
        document.getElementById('contactsList').innerHTML = `
            <div class="p-4 text-center text-red-500">
                <p>Error al cargar contactos</p>
                <p class="text-sm">${error.message}</p>
            </div>
        `;
    }
}

/**
 * Fallback del deep link: el contacto no está en allContacts tras la carga inicial.
 *
 * Llama GET /contacts/{phone}/hydrate?ensure_panel=true para obtener todos los
 * metadatos REALES del contacto (contact_id, display_name, owner, canal, stage)
 * en UN solo round-trip. El endpoint además re-inyecta el contacto al panel vía
 * _hydrate_contact_and_ensure_panel() — siguiente poll ya lo verá.
 *
 * Reemplaza el viejo flujo take-control + reload + fallback selectContact('',...)
 * que producía estado "ghost" cuando el contacto no se materializaba.
 *
 * @param {string} phone - Teléfono normalizado (con +)
 */
async function _handleDeepLinkMiss(phone) {
    // Normalización defensiva: asegurar formato +E164
    let phoneNorm = (phone || '').replace(/\s+/g, '').trim();
    if (phoneNorm && !phoneNorm.startsWith('+') && /^\d/.test(phoneNorm)) {
        phoneNorm = '+' + phoneNorm;
    }
    if (!phoneNorm) {
        console.error('[Panel] Deep link: phone vacío, abortando');
        return;
    }

    console.log('[Panel] Deep link: hidratando contacto cross-advisor:', phoneNorm);
    showToast('Cargando conversación...', 'info');

    try {
        const hydrateUrl =
            `${BASE_URL}/contacts/${encodeURIComponent(phoneNorm)}/hydrate` +
            `?ensure_panel=true`;

        const resp = await fetch(hydrateUrl, {
            headers: { 'X-API-Key': API_KEY }
        });

        if (resp.status === 404) {
            console.warn('[Panel] Deep link: contacto no existe en Redis/HubSpot:', phoneNorm);
            showToast('Este número no tiene historial en el sistema', 'error');
            return;
        }

        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.detail || `hydrate HTTP ${resp.status}`);
        }

        const contact = await resp.json();
        console.log('[Panel] Deep link hidratado:', {
            phone: contact.phone,
            name: contact.display_name,
            owner: contact.owner_id,
            canal: contact.canal,
            source: contact.source
        });

        // Inyectar en allContacts si aún no está (el próximo poll lo traerá, pero
        // el usuario no debe esperar al poll)
        if (!allContacts.some(c => c.phone === contact.phone)) {
            // Marcar cross_advisor si el owner real no es la asesora actual
            contact.cross_advisor = !!(
                ADVISOR_ID && contact.owner_id && String(contact.owner_id) !== String(ADVISOR_ID)
            );
            allContacts.unshift(contact);
            try { _applyFiltersAndRender(); } catch (_e) {}
        }

        // Mensaje informativo si el contacto es de otra asesora (cross-advisor)
        if (contact.owner_name &&
            ADVISOR_ID &&
            contact.owner_id &&
            String(contact.owner_id) !== String(ADVISOR_ID)) {
            showToast(`Contacto de: ${contact.owner_name}`, 'info');
        }

        // Seleccionar con TODOS los campos reales — nunca más phone como fallback de nombre
        selectContact(
            contact.contact_id || '',
            contact.phone,
            contact.display_name || contact.phone,
            contact.canal || contact.canal_origen || 'whatsapp'
        );

    } catch (err) {
        console.error('[Panel] Deep link: error hidratando:', err);
        showToast(`No se pudo cargar la conversación: ${err.message}`, 'error');
    }
}

async function loadChatHistory(contactId) {
    // Capturar el contactId y phone al momento de iniciar la petición
    // para verificar que no cambió durante el fetch (race condition fix)
    const requestedContactId = contactId;
    const requestedPhone = currentPhone;
    const requestedCanal = currentCanal;

    // ⚠️ 2026-06-02: reset del state de paginación al cambiar conversación.
    // Garantiza que el cursor (oldestTs) y hasMore correspondan SIEMPRE al
    // contacto actualmente abierto. Sin esto, scroll en conversación nueva
    // podría usar oldestTs de la conversación anterior.
    _resetChatHistoryState(requestedContactId, requestedCanal, requestedPhone);

    console.log('[Panel] Cargando historial para contact_id:', requestedContactId, 'canal:', requestedCanal, 'phone:', requestedPhone);
    try {
        // ⚠️ 2026-06-02: paginación cursor-based con scroll infinito.
        // Carga inicial = 100 mensajes (los más recientes). Si el usuario scrollea
        // hacia arriba, loadOlderMessages() trae páginas adicionales de 100 con
        // ?before_ts=<oldest_ts>. Esto evita transferir miles de mensajes upfront.
        let historyUrl = `${BASE_URL}/history/${requestedContactId}?limit=${CHAT_PAGE_SIZE}`;
        if (requestedCanal) {
            historyUrl += `&canal=${encodeURIComponent(requestedCanal)}`;
        }
        if (requestedPhone) {
            historyUrl += `&phone=${encodeURIComponent(requestedPhone)}`;
        }

        const response = await fetch(historyUrl, {
            headers: { 'X-API-Key': API_KEY }
        });

        // ═══════════════════════════════════════════════════════════════
        // FIX RACE CONDITION: Verificar que el contacto no cambió
        // mientras la petición estaba en vuelo. Si cambió, descartar.
        // ═══════════════════════════════════════════════════════════════
        if (currentContactId !== requestedContactId) {
            console.warn(`[Panel] Race condition detectada: petición para ${requestedContactId} descartada (ahora es ${currentContactId})`);
            return; // Descartar respuesta obsoleta
        }

        console.log('[Panel] Respuesta de historial:', response.status);

        // P2-C: verificar HTTP status antes de parsear — distingue error de "sin mensajes"
        if (!response.ok) {
            console.error(`[Panel] Error cargando historial: HTTP ${response.status}`);
            const chatMessages = document.getElementById('chatMessages');
            if (chatMessages) {
                chatMessages.innerHTML = `
                    <div class="flex flex-col items-center justify-center h-full text-red-500 py-8 text-sm gap-2">
                        <span>Error al cargar el historial (${response.status})</span>
                        <button onclick="loadChatHistory('${requestedContactId}')"
                                class="text-blue-500 underline text-xs">Reintentar</button>
                    </div>`;
            }
            return;
        }

        const data = await response.json();
        console.log('[Panel] Datos recibidos para', requestedContactId, '- mensajes:', data.messages?.length || 0);

        // Doble verificación después del JSON parse
        if (currentContactId !== requestedContactId) {
            console.warn(`[Panel] Race condition (post-parse): petición para ${requestedContactId} descartada`);
            return;
        }

        // Verificar si hay error en la respuesta (aunque sea 200)
        if (data.error) {
            console.warn('[Panel] Error en respuesta:', data.error);
        }

        // P2-B: HubSpot timeout — mostrar aviso de historial parcial + retry automático
        if (data.hs_timeout) {
            console.warn('[Panel] HubSpot timeout — historial puede ser parcial, reintentando en 8s');
            showToast('Cargando historial completo...', 'info');
            setTimeout(() => {
                if (currentContactId === requestedContactId) {
                    loadChatHistory(contactId);
                }
            }, 8000);
        }

        // Renderizar mensajes (puede estar vacio)
        renderChatBubbles(data.messages || []);

        // ⚠️ 2026-06-02: guardar cursor + has_more en state para paginación.
        // Solo si el contacto sigue siendo el mismo (race condition guard).
        if (currentContactId === requestedContactId) {
            chatHistoryState.hasMore = data.has_more === true;
            chatHistoryState.oldestTs = data.oldest_ts || null;
            // Inicializar cache de mensajes (necesario para _prependChatMessages).
            // El cache contiene los mensajes en orden cronológico exacto.
            window.__chatMessagesCache = (data.messages || []).slice();
            // Montar listener de scroll si aún no está montado.
            _attachScrollListener();
            // Si has_more=false y hay mensajes, mostrar separador de "inicio"
            if (!chatHistoryState.hasMore && (data.messages?.length || 0) > 0) {
                _showHistoryStartMarker();
            }
        }

        // Mostrar mensaje si no hay historial
        if (!data.messages || data.messages.length === 0) {
            console.log('[Panel] Sin mensajes en historial para canal:', requestedCanal);
        }

    } catch (error) {
        // Solo mostrar error si el contacto sigue siendo el mismo
        if (currentContactId === requestedContactId) {
            console.error('[Panel] Error cargando historial:', error);
            document.getElementById('chatMessages').innerHTML = `
                <div class="flex items-center justify-center h-full text-red-500">
                    <p>Error al cargar historial: ${error.message}</p>
                </div>
            `;
        }
    }
}

// =========================================================================
// ⚠️ 2026-06-02: PAGINACIÓN — Scroll infinito hacia el pasado
// =========================================================================
//
// Diseño:
//   • Scroll listener throttled (200ms) en #chatMessages.
//   • Trigger: scrollTop < SCROLL_THRESHOLD_PX Y state.hasMore Y !state.loading.
//   • Fetch /history/{cid}?before_ts=<oldestTs>&limit=100.
//   • Prepend mensajes ANTERIORES al inicio del DOM SIN saltos visuales:
//     guardamos scrollHeight antes, calculamos delta, restauramos scrollTop.
//   • Si recibimos menos de `limit` mensajes → has_more=false → desactivar trigger.
//   • Si el usuario cambia de conversación durante el fetch, descartamos resultado.
//
// Defensa en profundidad (6 capas):
//   1. state.loading (anti-doble-fetch)
//   2. state.hasMore=false (terminal: no más fetches)
//   3. Throttle scroll listener (max 5 evals/s)
//   4. Threshold 200px desde top (carga ANTES del límite visual)
//   5. Race condition check (contactId estable durante fetch)
//   6. renderChatBubbles deduplica por data-msg-id (defensa final natural)
// =========================================================================

function _attachScrollListener() {
    if (chatHistoryState.listenerAttached) return;
    const container = document.getElementById('chatMessages');
    if (!container) return;
    container.addEventListener('scroll', _onChatScroll);
    chatHistoryState.listenerAttached = true;
}

function _onChatScroll() {
    // Throttle: si ya hay un timeout pendiente, no encolar otro.
    if (chatHistoryState.scrollTimeout) return;
    chatHistoryState.scrollTimeout = setTimeout(() => {
        chatHistoryState.scrollTimeout = null;
        const container = document.getElementById('chatMessages');
        if (!container) return;
        if (container.scrollTop < SCROLL_THRESHOLD_PX) {
            loadOlderMessages();
        }
    }, SCROLL_THROTTLE_MS);
}

async function loadOlderMessages() {
    // Guards (capas 1, 2, 5 del diseño)
    if (chatHistoryState.loading) return;
    if (!chatHistoryState.hasMore) return;
    if (!chatHistoryState.oldestTs) return;
    if (!chatHistoryState.contactId) return;
    if (chatHistoryState.contactId !== currentContactId) return;

    const requestedCid = chatHistoryState.contactId;
    const requestedCursor = chatHistoryState.oldestTs;
    chatHistoryState.loading = true;
    _showLoadingMoreSpinner(true);

    try {
        let url = `${BASE_URL}/history/${requestedCid}?limit=${CHAT_PAGE_SIZE}`
                + `&before_ts=${encodeURIComponent(requestedCursor)}`;
        if (chatHistoryState.canal) {
            url += `&canal=${encodeURIComponent(chatHistoryState.canal)}`;
        }
        if (chatHistoryState.phone) {
            url += `&phone=${encodeURIComponent(chatHistoryState.phone)}`;
        }

        const response = await fetch(url, { headers: { 'X-API-Key': API_KEY } });
        if (!response.ok) {
            console.warn(`[Panel][Pagination] HTTP ${response.status} cargando página anterior`);
            return;
        }
        const data = await response.json();

        // Race guard (capa 5): si el usuario cambió de conversación, descartar.
        if (currentContactId !== requestedCid) {
            console.warn(`[Panel][Pagination] Contacto cambió durante fetch — descartando`);
            return;
        }

        const olderMessages = data.messages || [];
        if (olderMessages.length === 0) {
            chatHistoryState.hasMore = false;
            _showHistoryStartMarker();
            return;
        }

        // PREPEND con scroll-restore (sin salto visual)
        const container = document.getElementById('chatMessages');
        if (!container) return;

        // 1. Snapshot ANTES del prepend
        const scrollHeightBefore = container.scrollHeight;
        const scrollTopBefore = container.scrollTop;

        // 2. Actualizar cursor + hasMore PRIMERO (evita race con scroll
        //    durante el reflow: si el usuario scrollea más rápido que el
        //    layout, el guard `loading=true` sigue activo pero los flags
        //    ya están coherentes).
        chatHistoryState.hasMore = data.has_more === true;
        chatHistoryState.oldestTs = data.oldest_ts || null;

        // 3. Prepend (O(M) gracias al fix del 2026-06-04)
        _prependChatMessages(olderMessages);

        // 4. Forzar reflow antes de medir scrollHeight (el browser puede
        //    no haber recalculado layout tras el insertBefore masivo)
        void container.offsetHeight;
        const scrollHeightAfter = container.scrollHeight;
        container.scrollTop = scrollTopBefore + (scrollHeightAfter - scrollHeightBefore);

        if (!chatHistoryState.hasMore) {
            _showHistoryStartMarker();
        }

        console.log(`[Panel][Pagination] +${olderMessages.length} msgs anteriores cargados (total has_more=${chatHistoryState.hasMore})`);

    } catch (err) {
        console.warn('[Panel][Pagination] Error:', err);
    } finally {
        chatHistoryState.loading = false;
        _showLoadingMoreSpinner(false);
    }
}

// ⚠️ 2026-06-04: prepend O(M) — antes era O(N+M) con re-render total que
// causaba jank progresivo (1-5s) en chats largos. Ahora reutilizamos
// renderChatBubbles tal cual (cero duplicación) pero movemos los nodos
// recién creados al INICIO del container con prepend() nativo.
//
// Cache: window.__chatMessagesCache contiene los objetos msg en orden
// cronológico (más antiguo primero). Se inicializa en loadChatHistory().
function _prependChatMessages(messages) {
    const container = document.getElementById('chatMessages');
    if (!container) return;

    // Dedup contra cache existente (capa 6 del diseño)
    const cache = window.__chatMessagesCache || [];
    const existingIds = new Set(cache.map(m => String(m.id)));
    const trulyNew = messages.filter(m => m.id && !existingIds.has(String(m.id)));
    if (trulyNew.length === 0) return;

    // Actualizar cache cronológico (viejos primero)
    window.__chatMessagesCache = [...trulyNew, ...cache];

    // Limpiar marcador "inicio de conversación" si existía
    const oldMarker = document.getElementById('chatHistoryStartMarker');
    if (oldMarker) oldMarker.remove();

    // Snapshot: contar hijos actuales para identificar los recién insertados
    const beforeCount = container.children.length;

    // ⚠️ Flag para que renderChatBubbles NO dispare auto-scroll al fondo
    window.__isPaginating = true;
    try {
        // renderChatBubbles appendChild al FINAL (con dedup por data-msg-id)
        renderChatBubbles(trulyNew);
        // Mover los nodos recién agregados (del beforeCount en adelante) al INICIO
        const newNodes = Array.from(container.children).slice(beforeCount);
        if (newNodes.length > 0) container.prepend(...newNodes);
        // Eliminar date-dividers duplicados (puede haber 1 si el primer msg
        // viejo y el último msg nuevo cayeron en la misma fecha)
        const _seenDates = new Set();
        container.querySelectorAll('.date-divider').forEach(div => {
            if (_seenDates.has(div.dataset.date)) div.remove();
            else _seenDates.add(div.dataset.date);
        });
    } finally {
        // Limpiar flag tras el reflow (más rápido y robusto que setTimeout fijo)
        requestAnimationFrame(() => { window.__isPaginating = false; });
    }
}

function _showLoadingMoreSpinner(show) {
    const container = document.getElementById('chatMessages');
    if (!container) return;
    let spinner = document.getElementById('chatLoadingMoreSpinner');
    if (show) {
        if (!spinner) {
            // Skeleton: 5 burbujas-fantasma animadas (Tailwind animate-pulse)
            // alternando lado para imitar el flujo real cliente/asesora.
            // Da feedback visual instantáneo durante el fetch (200-500ms en
            // condiciones normales; en chats viejos sin caché puede llegar a 1-2s).
            spinner = document.createElement('div');
            spinner.id = 'chatLoadingMoreSpinner';
            spinner.className = 'space-y-3 py-3';
            const sides = ['justify-start', 'justify-end', 'justify-start', 'justify-end', 'justify-start'];
            const widths = ['w-32', 'w-48', 'w-40', 'w-24', 'w-56'];
            spinner.innerHTML = `
                <div class="flex items-center justify-center text-gray-400 text-xs mb-1">
                    <span class="animate-pulse">📜 Cargando mensajes anteriores...</span>
                </div>
                ${sides.map((side, i) => `
                    <div class="flex ${side} animate-pulse">
                        <div class="${widths[i]} h-8 bg-gray-200 rounded-lg"></div>
                    </div>
                `).join('')}
            `;
            container.insertBefore(spinner, container.firstChild);
        }
    } else {
        if (spinner) spinner.remove();
    }
}

function _showHistoryStartMarker() {
    const container = document.getElementById('chatMessages');
    if (!container) return;
    if (document.getElementById('chatHistoryStartMarker')) return; // ya existe
    const marker = document.createElement('div');
    marker.id = 'chatHistoryStartMarker';
    marker.className = 'flex items-center justify-center py-3 text-gray-400 text-xs';
    marker.innerHTML = '<span>📜 Inicio de la conversación</span>';
    container.insertBefore(marker, container.firstChild);
}

async function checkWindowStatus(phone) {
    console.log('[Panel] Verificando ventana 24h para:', phone);
    const windowWarning = document.getElementById('windowWarning');
    const messageInput = document.getElementById('messageInput');
    const sendBtn = document.getElementById('sendBtn');
    const templateSection = document.getElementById('templateSection');

    try {
        const response = await fetch(
            `${BASE_URL}/window-status/${encodeURIComponent(phone)}`,
            { headers: { 'X-API-Key': API_KEY } }
        );

        const data = await response.json();
        console.log('[Panel] Estado de ventana:', data);

        const statusDiv = document.getElementById('windowStatus');
        statusDiv.classList.remove('hidden');

        if (data.window_open) {
            // VENTANA ABIERTA: Habilitar texto libre
            statusDiv.className = 'text-sm bg-green-100 text-green-700 px-3 py-1 rounded-full';
            statusDiv.textContent = `Ventana: ${data.message}`;

            // Habilitar input de texto
            if (messageInput) {
                messageInput.disabled = false;
                messageInput.placeholder = 'Escribe un mensaje personalizado...';
                messageInput.classList.remove('bg-gray-200', 'cursor-not-allowed');
            }
            if (sendBtn) sendBtn.disabled = false;

            // Ocultar warning de ventana cerrada
            windowWarning.classList.add('hidden');

            // Templates siguen disponibles como opcion
            if (templateSection) templateSection.classList.remove('border-red-300', 'bg-red-50');

            _applyWindowClosedUI(false);
        } else {
            // VENTANA CERRADA: Solo templates
            statusDiv.className = 'text-sm bg-orange-100 text-orange-700 px-3 py-1 rounded-full';
            statusDiv.textContent = 'Ventana cerrada (>24h) - Usa template';

            // Deshabilitar input de texto
            if (messageInput) {
                messageInput.disabled = true;
                messageInput.placeholder = 'Ventana cerrada. Usa un template para reactivar.';
                messageInput.classList.add('bg-gray-200', 'cursor-not-allowed');
            }
            if (sendBtn) sendBtn.disabled = true;

            // Mostrar warning
            windowWarning.classList.remove('hidden');

            // Destacar seccion de templates
            if (templateSection) {
                templateSection.classList.remove('bg-blue-50', 'border-blue-200');
                templateSection.classList.add('bg-yellow-50', 'border-yellow-300');
            }

            _applyWindowClosedUI(true);
            console.warn('[Panel] Ventana de 24h cerrada. Ultimo mensaje:', data.last_message_time);
        }
    } catch (error) {
        console.error('[Panel] Error verificando ventana:', error);
        // En caso de error, permitir ambos metodos
        windowWarning.classList.add('hidden');
        const statusDiv = document.getElementById('windowStatus');
        statusDiv.classList.add('hidden');
        if (messageInput) messageInput.disabled = false;
        if (sendBtn) sendBtn.disabled = false;
    }
}

/**
 * Carga historial + estado de ventana en un solo round-trip via GET /contacts/{phone}/detail.
 * Reemplaza loadChatHistory() + checkWindowStatus() cuando se abre un contacto.
 * @param {string} phone
 * @param {string} contactId
 * @param {string|null} canal
 */
async function loadContactDetail(phone, contactId, canal) {
    const requestedContactId = contactId;

    const windowWarning = document.getElementById('windowWarning');
    const messageInput = document.getElementById('messageInput');
    const sendBtn = document.getElementById('sendBtn');
    const templateSection = document.getElementById('templateSection');

    // ⚠️ 2026-06-05: inicializar estado de paginación ANTES del fetch.
    // Sin esto, _attachScrollListener nunca se llama y el scroll infinito
    // queda muerto (loadOlderMessages aborta por guard de contactId/oldestTs).
    _resetChatHistoryState(contactId, canal, phone);

    try {
        // ⚠️ 2026-06-02: limit alineado con CHAT_PAGE_SIZE (100). Paginación
        // siguiente se hace vía loadOlderMessages() → GET /history/{cid}.
        let url = `${BASE_URL}/contacts/${encodeURIComponent(phone)}/detail?limit=${CHAT_PAGE_SIZE}`;
        if (contactId) url += `&contact_id=${encodeURIComponent(contactId)}`;
        if (canal) url += `&canal=${encodeURIComponent(canal)}`;

        const response = await fetch(url, { headers: { 'X-API-Key': API_KEY } });

        // Race condition: si el contacto cambió mientras esperábamos, descartar
        if (currentContactId !== requestedContactId) {
            console.warn(`[Panel] Race condition en loadContactDetail: descartado (${requestedContactId})`);
            return;
        }

        // P2-C: verificar HTTP status antes de parsear — distingue error de "sin mensajes"
        if (!response.ok) {
            console.error(`[Panel] Error en detail: HTTP ${response.status}`);
            renderChatBubbles([]);
            showToast(`Error cargando conversación (${response.status})`, 'error');
            return;
        }

        const data = await response.json();

        // Renderizar mensajes
        renderChatBubbles(data.messages || []);

        // ⚠️ 2026-06-05: engachar scroll infinito al sistema de paginación.
        // Sin esto, mi fix de _prependChatMessages O(M) queda huérfano.
        chatHistoryState.hasMore = data.has_more === true;
        chatHistoryState.oldestTs = data.oldest_ts || null;
        window.__chatMessagesCache = (data.messages || []).slice();
        _attachScrollListener();
        if (!chatHistoryState.hasMore && (data.messages?.length || 0) > 0) {
            _showHistoryStartMarker();
        }
        console.log(`[Panel][Pagination] Init detail: has_more=${chatHistoryState.hasMore} oldest_ts=${chatHistoryState.oldestTs} msgs=${data.messages?.length || 0}`);

        // Aplicar estado de ventana 24h
        const statusDiv = document.getElementById('windowStatus');
        statusDiv.classList.remove('hidden');

        if (data.window_open) {
            statusDiv.className = 'text-sm bg-green-100 text-green-700 px-3 py-1 rounded-full';
            statusDiv.textContent = `Ventana: ${data.window_message}`;
            if (messageInput) {
                messageInput.disabled = false;
                messageInput.placeholder = 'Escribe un mensaje personalizado...';
                messageInput.classList.remove('bg-gray-200', 'cursor-not-allowed');
            }
            if (sendBtn) sendBtn.disabled = false;
            windowWarning.classList.add('hidden');
            if (templateSection) templateSection.classList.remove('border-red-300', 'bg-red-50');
            _applyWindowClosedUI(false);
        } else {
            statusDiv.className = 'text-sm bg-orange-100 text-orange-700 px-3 py-1 rounded-full';
            statusDiv.textContent = 'Ventana cerrada (>24h) - Usa template';
            if (messageInput) {
                messageInput.disabled = true;
                messageInput.placeholder = 'Ventana cerrada. Usa un template para reactivar.';
                messageInput.classList.add('bg-gray-200', 'cursor-not-allowed');
            }
            if (sendBtn) sendBtn.disabled = true;
            windowWarning.classList.remove('hidden');
            if (templateSection) {
                templateSection.classList.remove('bg-blue-50', 'border-blue-200');
                templateSection.classList.add('bg-yellow-50', 'border-yellow-300');
            }
            _applyWindowClosedUI(true);
        }

    } catch (error) {
        if (currentContactId !== requestedContactId) return;
        console.error('[Panel] Error en loadContactDetail:', error);
        // Fallback: mostrar error en chat, habilitar inputs
        document.getElementById('chatMessages').innerHTML =
            `<div class="flex items-center justify-center h-full text-red-500"><p>Error al cargar: ${error.message}</p></div>`;
        windowWarning.classList.add('hidden');
        document.getElementById('windowStatus').classList.add('hidden');
        if (messageInput) messageInput.disabled = false;
        if (sendBtn) sendBtn.disabled = false;
    }
}

// =========================================================================
// FUNCIONES DE RENDERIZADO
// =========================================================================

// =========================================================================
// HELPERS PARA RENDERIZADO DIFERENCIAL DE CONTACTOS
// =========================================================================

/**
 * Genera el HTML del dropdown de pipeline para un contacto.
 * Extraído de la función anidada original para poder usarse en _buildContactHTML.
 */
const _CANAL_OPTIONS = [
    { value: 'whatsapp',        label: 'WhatsApp' },
    { value: 'whatsapp_directo',label: 'WhatsApp Directo' },
    { value: 'instagram',       label: 'Instagram' },
    { value: 'facebook',        label: 'Facebook' },
    { value: 'tiktok',          label: 'TikTok' },
    { value: 'linkedin',        label: 'LinkedIn' },
    { value: 'youtube',         label: 'YouTube' },
    { value: 'metrocuadrado',   label: 'MetroCuadrado' },
    { value: 'finca_raiz',      label: 'Finca Raíz' },
    { value: 'mercado_libre',   label: 'Mercado Libre' },
    { value: 'ciencuadras',     label: 'CienCuadras' },
    { value: 'pagina_web',      label: 'Página Web' },
    { value: 'desconocido',     label: 'Desconocido' },
];

function _buildCanalDropdown(contact) {
    // Usa canal_display si existe (editado por asesor), sino canal_origen
    const activeCanal = contact.canal_display || contact.canal_origen || 'whatsapp';
    const phone = contact.phone || '';
    const canal = contact.canal || contact.canal_origen || 'whatsapp';
    const options = _CANAL_OPTIONS.map(opt =>
        `<option value="${opt.value}" ${opt.value === activeCanal ? 'selected' : ''}>${opt.label}</option>`
    ).join('');
    return `<select class="canal-pill-select"
                data-canal-phone="${phone}"
                onchange="updateCanalDisplay(this.value, '${canal}')"
                onclick="event.stopPropagation()"
                title="Cambiar canal del contacto">${options}</select>`;
}

function _buildPipelineDropdown(contactIdForDropdown, currentStageForDropdown) {
    if (!contactIdForDropdown) return '';
    const options = PIPELINE_STAGES.map(stage =>
        `<option value="${stage.id}" ${stage.id === currentStageForDropdown ? 'selected' : ''}>${stage.name}</option>`
    ).join('');
    return `<select class="stage-pill-select"
                data-contact-id="${contactIdForDropdown}"
                onchange="updateDealStage('${contactIdForDropdown}', this.value)"
                onclick="event.stopPropagation()"
                title="Cambiar etapa">${options}</select>`;
}

/**
 * Genera un string "fingerprint" de todos los campos que afectan el render de un contacto.
 * Si el fingerprint no cambió, el elemento DOM no necesita ser reconstruido.
 */
function _getContactFingerprint(contact) {
    const contactId = contact.contact_id || contact.id || '';
    const phone = contact.phone || '';
    const cacheKey = contactId || phone;
    const cached = contactDealCache[cacheKey];
    const currentStage = contact.current_stage || (cached?.current_stage) || '';
    // Fix: time_ago se excluye del fingerprint — cambia cada minuto y forzaba reconstruir
    // el elemento DOM completo (incluyendo re-descarga del avatar). Se actualiza in-place
    // via _updateContactTimeAgo() al final de renderContactsList.
    return [
        contact.conversation_status || contact.status || '',
        contact.is_active ? '1' : '0',
        contact.display_name || '',
        contact.canal_origen || '',
        currentStage,
        contact.handoff_reason || '',
        contact.has_appointment ? '1' : '0',
        contactId === currentContactId ? 'active' : '',
        contact.last_message_preview || '',
    ].join('|');
}

/**
 * Actualiza el texto "time_ago" de un contacto en el DOM sin reconstruir el elemento.
 * Busca el span/p con data-time-ago dentro del contact-item del phone dado.
 */
function _updateContactTimeAgo(phone, timeAgo) {
    if (!timeAgo) return;
    const el = document.querySelector(`.contact-item[data-phone="${phone}"] [data-time-ago]`);
    if (el && el.textContent !== `Llego ${timeAgo}`) {
        el.textContent = `Llego ${timeAgo}`;
    }
}

/**
 * Genera el HTML completo para un único elemento de contacto en la lista.
 * Contiene la misma lógica que el antiguo .map() callback de renderContactsList.
 */
function _buildContactHTML(contact) {
    const isActive = contact.is_active === true;
    const status = contact.conversation_status || contact.status || '';
    const isInConversation = status === 'IN_CONVERSATION';
    const isHumanActive = status === 'HUMAN_ACTIVE' || status === 'PENDING_HANDOFF';
    const contactId = contact.contact_id || contact.id || '';
    const phone = contact.phone || '';
    const displayName = contact.display_name || 'Sin nombre';
    const canalOrigen = contact.canal_origen || '';

    let bgClass = '';
    if (status === 'PENDING_HANDOFF') {
        bgClass = 'contact-state-pending';
    } else if (isInConversation) {
        bgClass = 'contact-state-in-conversation';
    } else if (isHumanActive || isActive) {
        bgClass = 'contact-state-human';
    }

    const timeAgo = contact.time_ago || '';

    const canalColors = {
        'instagram': 'bg-pink-100 text-pink-700',
        'facebook': 'bg-blue-100 text-blue-700',
        'finca_raiz': 'bg-yellow-100 text-yellow-700',
        'metrocuadrado': 'bg-orange-100 text-orange-700',
        'pagina_web': 'bg-indigo-100 text-indigo-700',
        'whatsapp_directo': 'bg-green-100 text-green-700',
        'ciencuadras': 'bg-cyan-100 text-cyan-700',
        'charly': 'bg-violet-100 text-violet-700',
        'tiktok': 'bg-gray-900 text-white',
        'default': 'bg-gray-100 text-gray-600'
    };
    const canalColorClass = canalColors[canalOrigen] || canalColors['default'];
    const canalLabel = CANAL_LABELS[canalOrigen] || (canalOrigen ? canalOrigen.slice(0, 3).toUpperCase() : '');
    const canalBadge = canalLabel
        ? `<span class="text-xs ${canalColorClass} px-1.5 py-0.5 rounded font-semibold">${canalLabel}</span>`
        : '';

    const cacheKey = contactId || phone;
    let currentStage = contact.current_stage || contactDealCache[cacheKey]?.current_stage || '';

    // Actualizar caché local de stage para evitar flickering en re-renders
    if (cacheKey && contact.current_stage) {
        contactDealCache[cacheKey] = { current_stage: contact.current_stage };
    }

    // Fila 2 de la card: canal badge + stage select (o badge estático)
    let stageRow = '';
    if (isInConversation || isHumanActive || isActive) {
        const pipelineDropdown = _buildPipelineDropdown(contactId, currentStage);
        stageRow = pipelineDropdown || `<span class="text-xs bg-yellow-100 text-yellow-800 px-2 py-0.5 rounded-full animate-pulse">En espera</span>`;
    } else if (status === 'BOT_ACTIVE') {
        stageRow = currentStage
            ? `<span class="text-xs text-gray-500">${PIPELINE_STAGES.find(s => s.id === currentStage)?.name || ''}</span>`
            : '';
    } else {
        stageRow = `<span class="text-xs bg-gray-100 text-gray-500 px-2 py-0.5 rounded-full">Historial</span>`;
    }

    const bgColors = ['10B981', '3B82F6', 'F59E0B', 'EF4444', '8B5CF6', 'EC4899', '06B6D4'];
    const colorIndex = (displayName || 'A').charCodeAt(0) % bgColors.length;
    const bgColor = isInConversation ? '3B82F6' : (isHumanActive || isActive) ? '10B981' : bgColors[colorIndex];
    const avatarUrl = `https://ui-avatars.com/api/?name=${encodeURIComponent(displayName || '?')}&background=${bgColor}&color=fff&size=40&rounded=true&bold=true`;

    const unread = unreadCounts[phone] || 0;
    const unreadBadge = (unread > 0 && phone !== currentPhone)
        ? `<span class="unread-badge absolute -top-1 -right-1 bg-red-500 text-white text-xs rounded-full min-w-[18px] h-[18px] flex items-center justify-center font-bold px-0.5 leading-none">${unread > 9 ? '9+' : unread}</span>`
        : '';

    // Punto azul: "marcado como no leído" manualmente (solo si no hay badge rojo real)
    const manualUnreadBadge = (_manuallyUnread.has(phone) && phone !== currentPhone && unread === 0)
        ? `<span class="manual-unread-badge absolute -top-1 -right-1 bg-blue-500 rounded-full w-[12px] h-[12px]" title="Marcado como no leído"></span>`
        : '';

    const apptBadge = contact.has_appointment
        ? `<span class="absolute -bottom-1 -right-1 bg-amber-400 text-white text-xs rounded-full w-[18px] h-[18px] flex items-center justify-center leading-none" title="Tiene cita programada">📅</span>`
        : '';

    // [Sync] Badge cross-advisor: bottom-left (slot libre) — contacto reasignado a otro asesor
    const crossAdvisorBadge = contact.cross_advisor
        ? `<span class="absolute -bottom-1 -left-1 bg-sky-400 text-white text-xs rounded-full w-[18px] h-[18px] flex items-center justify-center leading-none" title="Asignado a otro asesor">🔄</span>`
        : '';

    // [StageFilter] Badge HubSpot-only: contacto desde filtro de etapa sin conversación activa en Redis
    const hsOnlyBadge = contact.from_hubspot_stage
        ? `<span class="absolute -top-1 -right-1 bg-amber-400 text-white text-[10px] rounded px-1 py-0.5 leading-none" title="Sin conversación activa — solo HubSpot">🏷</span>`
        : '';

    const stateBadge = (isInConversation || isHumanActive || isActive)
        ? `<span class="absolute -top-1 -left-1 bg-blue-100 rounded-full w-[18px] h-[18px] flex items-center justify-center text-[10px] leading-none" title="Asesora atendiendo">👤</span>`
        : status === 'BOT_ACTIVE'
        ? `<span class="absolute -top-1 -left-1 bg-gray-100 rounded-full w-[18px] h-[18px] flex items-center justify-center text-[10px] leading-none" title="Sofía está manejando">🤖</span>`
        : '';

    // Fila 3: Preview del último mensaje (estilo WhatsApp)
    const msgPreview = contact.last_message_preview || '';
    const msgSender = contact.last_message_sender || '';
    let senderPrefix = '';
    if (msgSender === 'advisor') senderPrefix = '<span class="text-gray-600 font-medium">Tú: </span>';
    else if (msgSender === 'bot') senderPrefix = '<span class="text-gray-600 font-medium">SofIA: </span>';
    const previewRow = msgPreview
        ? `<p class="text-xs text-gray-500 truncate mt-0.5">${senderPrefix}${escapeHtml(msgPreview)}</p>`
        : '';

    // Layout 3 filas: [Fila 1] nombre + timeAgo  |  [Fila 2] canal badge + stage  |  [Fila 3] preview
    return `
        <div class="contact-item p-2.5 border-b cursor-pointer ${bgClass} ${contactId === currentContactId ? 'active' : ''}"
             data-phone="${phone}"
             onclick="selectContact('${contactId}', '${phone}', '${displayName.replace(/'/g, "\\'")}', '${canalOrigen}')">
            <div class="flex items-center gap-2.5">
                <div class="relative flex-shrink-0">
                    <img src="${avatarUrl}"
                         class="w-10 h-10 rounded-full"
                         alt="${displayName}"
                         onerror="this.onerror=null; this.src='https://ui-avatars.com/api/?name=%3F&background=gray&color=fff&size=40&rounded=true';">
                    ${stateBadge}
                    ${unreadBadge}
                    ${manualUnreadBadge}
                    ${apptBadge}
                    ${crossAdvisorBadge}
                    ${hsOnlyBadge}
                </div>
                <div class="flex-1 min-w-0">
                    <div class="flex items-center justify-between gap-1">
                        <p class="font-semibold text-sm text-gray-900 truncate">${displayName}</p>
                        ${timeAgo ? `<span class="text-xs text-gray-400 flex-shrink-0" data-time-ago>Llego ${timeAgo}</span>` : ''}
                    </div>
                    <div class="flex items-center gap-1 mt-0.5 min-w-0">
                        ${canalBadge}
                        <div class="min-w-0 overflow-hidden">${stageRow}</div>
                    </div>
                    ${previewRow}
                    ${contact.handoff_reason ? `<p class="text-xs text-gray-400 truncate mt-0.5">${contact.handoff_reason}</p>` : ''}
                    ${contact.cross_advisor && contact.owner_name ? `<p class="text-xs text-sky-600 truncate mt-0.5 font-medium" title="Asesora asignada">👤 ${contact.owner_name}</p>` : ''}
                </div>
            </div>
        </div>
    `;
}

// =========================================================================
// RENDERIZADO DE LISTA DE CONTACTOS — Diferencial (sin innerHTML completo)
// =========================================================================

/**
 * Renderiza la lista de contactos de forma diferencial:
 * solo reconstruye los elementos cuyo fingerprint cambió,
 * inserta los nuevos y elimina los que ya no están.
 * Evita el parpadeo causado por reemplazar container.innerHTML completo.
 */
function renderContactsList(contacts) {
    // [Bug2] Guard: si hay un render en curso, encolar el más reciente y salir.
    // Cuando el render activo termine (requestAnimationFrame callback), procesará el encolado.
    if (_renderInProgress) {
        _pendingRenderContacts = contacts;
        return;
    }
    _renderInProgress = true;
    requestAnimationFrame(() => {
        _renderContactsListInner(contacts);
        _renderInProgress = false;
        if (_pendingRenderContacts !== null) {
            const _next = _pendingRenderContacts;
            _pendingRenderContacts = null;
            renderContactsList(_next);
        }
    });
}

function _renderContactsListInner(contacts) {
    const container = document.getElementById('contactsList');

    if (!contacts || contacts.length === 0) {
        const _currentDate = document.getElementById('dateFrom')?.value;
        const _searchTerm = (window._currentSearchTerm || '').trim();
        let _emptyMsg;
        if (_searchTerm) {
            // Búsqueda activa: mensaje contextual
            const _safeTerm = _searchTerm.replace(/[<>&]/g, ch => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[ch]));
            // Si parece teléfono, dar feedback específico
            const _looksLikePhone = /^\+?[\d\s\-()]+$/.test(_searchTerm) &&
                                    _searchTerm.replace(/[^\d]/g, '').length >= 10;
            _emptyMsg = _looksLikePhone
                ? `No se encontró ningún contacto con el número <strong>${_safeTerm}</strong>.<br><span class="text-xs text-gray-500">Verifica que el número esté en HubSpot o intenta con el código de país.</span>`
                : `Sin resultados para <strong>"${_safeTerm}"</strong>.<br><span class="text-xs text-gray-500">Intenta con otro término o parte del nombre.</span>`;
        } else if (activeWorkerFilter && activeWorkerName) {
            _emptyMsg = `No hay citas de <strong>${activeWorkerName}</strong> en el período seleccionado.`;
        } else if (_currentDate) {
            const [_y, _m, _d] = _currentDate.split('-');
            const _meses = ['ene','feb','mar','abr','may','jun','jul','ago','sep','oct','nov','dic'];
            const _fechaDisplay = `${parseInt(_d)} ${_meses[parseInt(_m) - 1]}. ${_y}`;
            const _fieldEl = document.querySelector('input[name="dateField"]:checked');
            const _field = _fieldEl ? _fieldEl.value : 'last_activity';
            _emptyMsg = _field === 'created_at'
                ? `No hay contactos que llegaron el <strong>${_fechaDisplay}</strong>.`
                : `No hay contactos con actividad el <strong>${_fechaDisplay}</strong>.`;
        } else {
            _emptyMsg = 'No hay contactos esperando atención.';
        }
        container.innerHTML = `
            <div class="p-4 text-center text-gray-400 empty-state-msg">
                <p>${_emptyMsg}</p>
            </div>
        `;
        _contactFingerprints.clear();
        return;
    }

    // FIX-G: limpiar cualquier empty-state residual de renders previos que no
    // fueron reemplazados (por ej. cuando transicionamos de 0 resultados a N).
    // El diff-renderer sólo gestiona nodos con [data-phone], así que un div de
    // empty-state queda flotando como "item fantasma" entre contactos.
    for (const el of container.querySelectorAll('.empty-state-msg')) {
        el.remove();
    }

    const savedScrollTop = container.scrollTop;
    // Fix-C: detectar cambio en índice 0 para forzar scroll al top (comportamiento WhatsApp)
    const prevTopPhone = window._lastTopPhone || null;
    const newTopPhone = contacts.length > 0 ? (contacts[0].phone || '') : '';

    // Construir mapa de elementos existentes en el DOM indexados por phone
    const existingMap = new Map();
    for (const el of container.querySelectorAll('.contact-item[data-phone]')) {
        existingMap.set(el.dataset.phone, el);
    }

    const newPhones = new Set(contacts.map(c => c.phone || ''));

    // Eliminar contactos que ya no están en la lista nueva
    for (const [phone, el] of existingMap) {
        if (!newPhones.has(phone)) {
            el.remove();
            existingMap.delete(phone);
            _contactFingerprints.delete(phone);
        }
    }

    // Actualizar o insertar cada contacto en el orden correcto
    for (let i = 0; i < contacts.length; i++) {
        const contact = contacts[i];
        const phone = contact.phone || '';
        const fingerprint = _getContactFingerprint(contact);
        const existing = existingMap.get(phone);

        let el;
        if (existing) {
            if (_contactFingerprints.get(phone) !== fingerprint) {
                // Los datos cambiaron — reconstruir solo este elemento
                const wrapper = document.createElement('div');
                wrapper.innerHTML = _buildContactHTML(contact).trim();
                const newEl = wrapper.firstElementChild;
                existing.replaceWith(newEl);
                existingMap.set(phone, newEl);
                el = newEl;
            } else {
                el = existing; // Sin cambios, reutilizar
            }
            _contactFingerprints.set(phone, fingerprint);
        } else {
            // Contacto nuevo — crear e insertar
            const wrapper = document.createElement('div');
            wrapper.innerHTML = _buildContactHTML(contact).trim();
            el = wrapper.firstElementChild;
            existingMap.set(phone, el);
            _contactFingerprints.set(phone, fingerprint);
        }

        // Garantizar posición correcta en el contenedor (orden del servidor)
        const currentAtPosition = container.children[i];
        if (currentAtPosition !== el) {
            container.insertBefore(el, currentAtPosition || null);
        }

        // Fix CR-3: re-aplicar badge si el contacto tiene no-leídos pendientes en memoria.
        // updateUnreadBadge() hace early-return si el elemento no existe → es seguro llamarlo siempre.
        if (unreadCounts[phone] > 0) {
            updateUnreadBadge(phone, unreadCounts[phone]);
        }

        // Fix time_ago: actualizar in-place sin reconstruir el elemento DOM
        if (contact.time_ago) {
            _updateContactTimeAgo(phone, contact.time_ago);
        }
    }

    // Fix-C: Si el índice 0 cambió (llegó mensaje nuevo que subió un contacto al top),
    // scrollear al inicio. Si no cambió, restaurar posición para no interrumpir al asesor.
    if (newTopPhone && newTopPhone !== prevTopPhone && prevTopPhone !== null) {
        container.scrollTop = 0;
        console.log(`[Panel][Scroll] Índice 0 cambió: ${prevTopPhone} → ${newTopPhone}, scrolleando al top`);
    } else {
        container.scrollTop = savedScrollTop;
    }
    window._lastTopPhone = newTopPhone;
}

// Variable para tracking de primera carga
let isFirstChatLoad = true;
// Variable para tracking del contacto actual en el chat (para detectar cambio de contacto)
let renderedContactId = null;

function renderChatBubbles(messages) {
    const container = document.getElementById('chatMessages');

    // =========================================================================
    // FIX CRUCE DE CONVERSACIONES: Detectar cambio de contacto
    // Si el contacto cambió, LIMPIAR completamente el contenedor antes de renderizar
    // =========================================================================
    const contactChanged = renderedContactId !== currentContactId;
    if (contactChanged) {
        console.log(`[Panel] Contacto cambió: ${renderedContactId} → ${currentContactId}, limpiando chat`);
        container.innerHTML = '';  // Limpiar mensajes del contacto anterior
        renderedContactId = currentContactId;
    }

    if (!messages || messages.length === 0) {
        // Mostrar mensaje de sin historial (siempre al limpiar o si está vacío)
        container.innerHTML = `
            <div class="flex items-center justify-center h-full text-gray-500" data-empty-msg="true">
                <p>No hay mensajes en el historial</p>
            </div>
        `;
        return;
    }

    // SINCRONIZACION INCREMENTAL: Solo agregar mensajes nuevos (para el mismo contacto)
    // Esto evita el parpadeo causado por innerHTML = ''
    let hasNewContent = false;

    // Remover mensaje vacio si existe
    const emptyMsg = container.querySelector('[data-empty-msg]');
    if (emptyMsg) {
        emptyMsg.remove();
    }

    // Limpiar burbujas optimistas — serán reemplazadas por los datos reales de MongoDB
    container.querySelectorAll('[data-optimistic="true"]').forEach(el => el.remove());

    // DATE DIVIDERS: rastrear último día renderizado (soporta actualizaciones incrementales)
    const _existingDividers = container.querySelectorAll('.date-divider');
    let lastRenderedDate = _existingDividers.length > 0
        ? _existingDividers[_existingDividers.length - 1].dataset.date
        : null;

    // Nombre real del lead (para reemplazar "Cliente" hardcoded en sender_name)
    const _contactHeaderName = (document.getElementById('contactName')?.textContent || '').trim();
    const _clientDisplayName = (_contactHeaderName && _contactHeaderName !== 'Selecciona un contacto')
        ? _contactHeaderName.split(' ')[0]
        : 'Cliente';

    // Patrones de sufijos/prefijos a limpiar en render-time
    const _SOURCE_TAG_RX = /\n*\[Fuente:[^\]]*\]\s*$/i;
    const _TEMPLATE_TAG_RX = /^\[TEMPLATE:\s*([^\]]+)\]\s*/i;
    const _BULK_TAG_RX = /^\[BULK template:\s*([^\]]+)\]\s*$/i;
    const _MEDIA_BODY_RX = [
        /^\[El cliente envió un audio[^\]]*\]\s*$/i,
        /^\[Imagen del cliente\]:\s*/i,
        /^\[Audio del cliente\]:\s*/i,
    ];

    // Tracking para agrupamiento por autor consecutivo
    const GROUP_WINDOW_MS = 120_000;
    let _prevSender = null;
    let _prevTs = 0;

    messages.forEach((msg, _idx) => {
        // Verificar si el mensaje ya existe en el DOM usando data-msg-id
        const existingMsg = container.querySelector(`[data-msg-id="${msg.id}"]`);
        const msgDate = formatBogotaDate(msg.timestamp);

        if (!existingMsg) {
            // Insertar separador de fecha si el día cambió
            if (msgDate && msgDate !== lastRenderedDate) {
                container.insertAdjacentHTML('beforeend', buildDateDivider(getDateLabel(msg.timestamp), msgDate));
            }

            // Solo renderizar e insertar si no existe
            const isRight = msg.align === 'right';
            let bubbleClass = 'bubble-advisor';  // default
            if (msg.sender === 'client') bubbleClass = 'bubble-client';
            else if (msg.sender === 'bot') bubbleClass = 'bubble-bot';
            else if (msg.sender === 'manual_note') bubbleClass = 'bubble-manual-note';
            else if (msg.sender === 'system') bubbleClass = 'bubble-system';
            else if (msg.sender === 'advisor') bubbleClass = 'bubble-advisor';

            const timestamp = msg.timestamp
                ? formatBogotaTime(msg.timestamp)
                : '';

            // Renderizar multimedia si existe
            // Soporta tanto el nuevo formato (msg.media.permanent_url) como legacy (msg.media_url)
            let mediaHtml = '';
            const mediaUrl = msg.media?.permanent_url || msg.media_url;
            const mediaType = msg.media?.type || msg.media_type;
            const transcription = msg.media?.transcription;
            const analysis = msg.media?.analysis;

            // Helper: detectar si la URL corresponde a un documento por extensión
            const _isDocUrl = (url) => url && /\.(pdf|docx?|xlsx?)(\?|$)/i.test(url);

            if (mediaUrl) {
                if (mediaType === 'image') {
                    mediaHtml = `
                        <div class="mb-2">
                            <img src="${mediaUrl}" alt="Imagen"
                                 class="max-w-full rounded-lg cursor-pointer hover:opacity-90 transition-opacity"
                                 style="max-height: 300px; object-fit: contain;"
                                 onclick="window.open('${mediaUrl}', '_blank')">
                        </div>`;
                    // Mostrar analisis de imagen si existe
                    if (analysis) {
                        mediaHtml += `
                            <div class="mt-1 p-2 bg-blue-50 rounded text-xs text-blue-700 italic">
                                <span class="font-medium">Analisis:</span> ${escapeHtml(analysis)}
                            </div>`;
                    }
                } else if (mediaType === 'audio') {
                    const _pid = `ap-${String(msg.id || Date.now()).replace(/\W/g,'')}-${Math.random().toString(36).slice(2,6)}`;
                    mediaHtml = buildAudioPlayerHtml(mediaUrl, _detectAudioType(mediaUrl), _pid);
                    // Mostrar transcripcion de audio si existe
                    if (transcription) {
                        mediaHtml += `
                            <div class="mt-1 p-2 bg-green-50 rounded text-xs text-green-700">
                                <span class="font-medium">Transcripcion:</span> ${escapeHtml(transcription)}
                            </div>`;
                    }
                } else if (mediaType === 'video') {
                    mediaHtml = `
                        <div class="mb-2">
                            <video src="${mediaUrl}" controls preload="metadata"
                                   class="max-w-full rounded-lg"
                                   style="max-height:300px;max-width:100%;">
                                Tu navegador no soporta video.
                            </video>
                        </div>`;
                } else if (mediaType === 'document' || _isDocUrl(mediaUrl)) {
                    // File Card para documentos (PDF, DOCX, XLSX)
                    const docFormat = msg.media?.doc_format
                        || (mediaUrl.match(/\.([a-z]+)(\?|$)/i) || [])[1]?.toLowerCase()
                        || 'doc';
                    const docIcon = msg.media?.doc_icon
                        || (docFormat === 'pdf' ? '📄' : (docFormat.includes('xl') ? '📊' : '📝'));
                    const fileName = msg.media?.original_filename
                        || decodeURIComponent(mediaUrl.split('/').pop().split('?')[0])
                        || 'Documento';

                    mediaHtml = `
                        <div class="mb-2 p-3 bg-gray-50 border border-gray-200 rounded-lg flex items-center gap-3"
                             style="max-width: 280px;">
                            <span style="font-size:1.5rem;line-height:1">${docIcon}</span>
                            <div style="flex:1;min-width:0;">
                                <p class="text-sm font-medium text-gray-800"
                                   style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"
                                   title="${escapeHtml(fileName)}">${escapeHtml(fileName)}</p>
                                <p class="text-xs text-gray-500" style="text-transform:uppercase;">${escapeHtml(docFormat)}</p>
                            </div>
                            <a href="${mediaUrl}" target="_blank" rel="noopener"
                               class="flex-shrink-0 px-3 py-1 bg-blue-600 text-white text-xs rounded-md hover:bg-blue-700 transition-colors font-medium"
                               style="white-space:nowrap;">
                                Ver
                            </a>
                        </div>`;
                } else {
                    // Archivo generico
                    mediaHtml = `
                        <div class="mb-2">
                            <a href="${mediaUrl}" target="_blank"
                               class="inline-flex items-center text-blue-600 hover:text-blue-800">
                                <svg class="w-4 h-4 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                                          d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"></path>
                                </svg>
                                Ver archivo
                            </a>
                        </div>`;
                }
            }

            // Detectar audio guardado como texto plano: "🎵 Audio: https://..."
            // Ocurre cuando MongoDB guarda el audio en msg.message en vez de msg.media
            let _displayMsg = msg.message || '';
            if (!mediaUrl && _displayMsg) {
                const _atm = _displayMsg.match(/^🎵\s*Audio:\s*(https?:\/\/\S+)([\s\S]*)?$/i);
                if (_atm) {
                    const _pid3 = `ap-txt-${String(msg.id || Date.now()).replace(/\W/g,'')}-${Math.random().toString(36).slice(2,6)}`;
                    mediaHtml = buildAudioPlayerHtml(_atm[1], _detectAudioType(_atm[1]), _pid3);
                    _displayMsg = (_atm[2] || '').trim();
                }
            }

            // Limpiar sufijo "[Fuente: ... Via Panel]" (defensa en profundidad: backend ya
            // lo filtra al hidratar desde HubSpot, pero por si llega de otra ruta).
            _displayMsg = _displayMsg.replace(_SOURCE_TAG_RX, '').trimEnd();

            // Parsear prefijo "[TEMPLATE: <nombre>]" → chip discreto sobre el cuerpo
            let templateBadge = '';
            const _tplMatch = _displayMsg.match(_TEMPLATE_TAG_RX);
            if (_tplMatch) {
                const _tplName = _tplMatch[1].trim();
                templateBadge = `<span class="tpl-chip" title="Plantilla: ${escapeHtml(_tplName)}">📋 Plantilla</span><br>`;
                _displayMsg = _displayMsg.replace(_TEMPLATE_TAG_RX, '');
            }

            // Parsear "[BULK template: <name>]" → chip "Masivo" + preview de plantilla como cuerpo
            const _bulkMatch = _displayMsg.match(_BULK_TAG_RX);
            if (_bulkMatch) {
                const _bulkName = _bulkMatch[1].trim();
                const _bulkTpl = BULK_TEMPLATES.find(t => t.name === _bulkName);
                if (_bulkTpl) {
                    // Sustituir {key} → etiqueta legible usando vars del template
                    let _bulkPreview = _bulkTpl.preview;
                    (_bulkTpl.vars || []).forEach(v => {
                        const _vLabel = v.auto_fill === 'firstname' ? '{nombre}' : `{${v.label.toLowerCase()}}`;
                        _bulkPreview = _bulkPreview.replace(`{${v.key}}`, _vLabel);
                    });
                    templateBadge = `<span class="bulk-chip" title="Plantilla: ${escapeHtml(_bulkTpl.label)}">📢 Masivo</span><br>`;
                    _displayMsg = _bulkPreview;
                } else {
                    // Legacy: campaign_id de MongoDB — no hay lookup posible
                    templateBadge = `<span class="bulk-chip" title="Mensaje masivo (plantilla no disponible)">📢 Masivo</span><br>`;
                    _displayMsg = '';
                }
            }

            // Suprimir cuerpo duplicado de multimedia: si ya hay chip de
            // transcripción/análisis y el body es el wrapper "[El cliente envió...]"
            // o "[Imagen del cliente]: ...", ocultarlo.
            if (transcription || analysis) {
                if (_MEDIA_BODY_RX.some(rx => rx.test(_displayMsg))) {
                    _displayMsg = '';
                } else {
                    _displayMsg = _displayMsg.replace(/^\[Imagen del cliente\]:\s*/i, '').trim();
                    if (analysis && _displayMsg.trim() === String(analysis).trim()) _displayMsg = '';
                    if (transcription && _displayMsg.trim() === String(transcription).trim()) _displayMsg = '';
                }
            }

            // --- QUOTE HTML (si este mensaje es respuesta a otro) ---
            let quoteHtml = '';
            if (msg.reply_to_id && msg.reply_to_preview) {
                const rp = msg.reply_to_preview;
                const rpSender = rp.sender_name || rp.sender || '';
                let rpText = escapeHtml((rp.content || '').substring(0, 80));
                if (rp.media_type && !rp.content) {
                    rpText = { image: '📷 Imagen', audio: '🎵 Audio', video: '🎬 Video', document: '📄 Documento' }[rp.media_type] || '📎 Archivo';
                }
                const rpColor = rp.sender === 'client' ? '#6B7280' : (rp.sender === 'bot' ? '#D97706' : '#2563EB');
                quoteHtml = `
                    <div class="reply-quote mb-2 p-2 rounded" style="background:rgba(0,0,0,0.04);border-left:3px solid ${rpColor};" onclick="scrollToMessage('${msg.reply_to_id}')">
                        <p class="text-xs font-semibold" style="color:${rpColor};">${escapeHtml(rpSender)}</p>
                        <p class="text-xs text-gray-500 truncate">${rpText}</p>
                    </div>`;
            }

            // --- REPLY BUTTON (inside bubble, visible on bubble hover via CSS) ---
            const replyBtnHtml = `
                <button class="reply-btn"
                        onclick="event.stopPropagation();selectReplyMessage('${msg.id}')" title="Responder">
                    <svg class="w-3.5 h-3.5 text-gray-400" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" d="M3 10h10a8 8 0 018 8v2M3 10l6 6m-6-6l6-6"/>
                    </svg>
                </button>`;

            // Menú ⋯ Editar/Eliminar — solo en mensajes propios del asesor (sender=advisor)
            const _isAdvisorMsg = (msg.sender === 'advisor');
            const ownerMenuHtml = _isAdvisorMsg ? `
                <button class="msg-menu-btn"
                        onclick="event.stopPropagation();toggleMsgMenu('${msg.id}', this)"
                        title="Más opciones"
                        style="position:absolute;top:6px;right:6px;background:transparent;border:none;cursor:pointer;padding:2px 4px;">
                    <svg class="w-4 h-4 text-gray-400 hover:text-gray-600" fill="currentColor" viewBox="0 0 20 20">
                        <path d="M10 3a1.5 1.5 0 110 3 1.5 1.5 0 010-3zm0 5.5a1.5 1.5 0 110 3 1.5 1.5 0 010-3zm0 5.5a1.5 1.5 0 110 3 1.5 1.5 0 010-3z"/>
                    </svg>
                </button>` : '';

            const _safeContent = (msg.message || '').replace(/"/g, '&quot;').replace(/'/g, '&#39;');

            // Mensajes eliminados: burbuja neutra sin contenido
            if (msg.deleted) {
                const _deletedHtml = `
                    <div class="flex ${isRight ? 'justify-end' : 'justify-start'} mb-3"
                         data-msg-id="${msg.id}">
                        <div class="bubble-deleted p-3 shadow-sm" style="background:#f3f4f6;border-radius:12px;max-width:70%;">
                            <p class="text-xs text-gray-400 italic">Mensaje eliminado</p>
                            <p class="text-xs text-gray-400 text-right mt-1">${timestamp}</p>
                        </div>
                    </div>`;
                container.insertAdjacentHTML('beforeend', _deletedHtml);
                hasNewContent = true;
                if (msgDate) lastRenderedDate = msgDate;
                return;
            }

            // Chip "(editado)" junto a la hora
            const _editedChip = msg.edited
                ? `<span class="text-xs text-gray-400 ml-1">(editado)</span>`
                : '';

            // Header: sustituir "Cliente" hardcoded por nombre real del lead
            let _headerName = msg.sender_name || msg.sender || '';
            if (msg.sender === 'client' && (!_headerName || _headerName === 'Cliente')) {
                _headerName = _clientDisplayName;
            }

            // Agrupamiento por autor consecutivo (omitir nombre y reducir margen)
            const _curTs = msg.timestamp ? new Date(msg.timestamp).getTime() : 0;
            const _isGrouped = _prevSender === msg.sender
                && _prevTs > 0 && _curTs > 0
                && Math.abs(_curTs - _prevTs) < GROUP_WINDOW_MS;
            const _groupClass = _isGrouped ? ' bubble-grouped' : '';
            const _headerHtml = _isGrouped
                ? ''
                : `<p class="text-xs font-semibold text-gray-600 mb-1">${escapeHtml(_headerName)}</p>`;

            // Status icons (✓ / ✓✓ / ✓✓ azul) sólo en mensajes salientes con msg.status
            let _statusHtml = '';
            if (isRight && msg.status) {
                const s = String(msg.status).toLowerCase();
                if (s === 'read') _statusHtml = '<span class="msg-status read">✓✓</span>';
                else if (s === 'delivered') _statusHtml = '<span class="msg-status">✓✓</span>';
                else if (s === 'sent' || s === 'queued' || s === 'sending') _statusHtml = '<span class="msg-status">✓</span>';
            }

            const msgHtml = `
                <div class="flex ${isRight ? 'justify-end' : 'justify-start'} mb-3 animate-fadeIn${_groupClass}"
                     data-msg-id="${msg.id}"
                     data-sender="${msg.sender || ''}"
                     data-sender-name="${escapeHtml(_headerName)}"
                     data-content="${_safeContent}"
                     data-media-type="${msg.media?.type || ''}"
                     data-timestamp="${msg.timestamp || ''}">
                    <div class="${bubbleClass} shadow-sm relative">
                        ${replyBtnHtml}
                        ${ownerMenuHtml}
                        ${_headerHtml}
                        ${templateBadge}
                        ${quoteHtml}
                        ${mediaHtml}
                        <p class="text-gray-800 whitespace-pre-wrap" data-msg-body="1" style="${_displayMsg ? '' : 'display:none;'}">${linkifyHtml(_displayMsg || '')}</p>
                        <p class="text-xs text-gray-500 text-right mt-1">${timestamp}${_editedChip}${_statusHtml}</p>
                    </div>
                </div>
            `;

            container.insertAdjacentHTML('beforeend', msgHtml);
            hasNewContent = true;

            _prevSender = msg.sender;
            _prevTs = _curTs;
        }

        // Actualizar lastRenderedDate para TODOS los mensajes (existentes y nuevos)
        if (msgDate) lastRenderedDate = msgDate;
    });

    // Solo hacer scroll si hay contenido nuevo o es la primera carga.
    // ⚠️ 2026-06-02: si estamos en modo paginación (cargando mensajes anteriores
    // via scroll infinito), NO hacer auto-scroll al fondo — el caller restaura
    // la posición manualmente con scrollTop = before + delta.
    const isPaginating = window.__isPaginating === true;
    if ((hasNewContent || isFirstChatLoad) && !isPaginating) {
        setTimeout(() => {
            container.scrollTo({
                top: container.scrollHeight,
                behavior: isFirstChatLoad ? 'auto' : 'smooth'
            });
        }, 100);
        isFirstChatLoad = false;
    }
}

function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

/**
 * Convierte URLs en el texto a elementos <a> clickeables en el panel.
 * Escapa primero cada segmento de texto para prevenir XSS.
 * Solo afecta la vista del panel (no modifica el mensaje enviado al cliente).
 */
function linkifyHtml(rawText) {
    if (!rawText) return '';
    const URL_RX = /(https?:\/\/[^\s<>"']{4,})/gi;
    const segments = rawText.split(URL_RX);
    return segments.map((seg, i) => {
        if (i % 2 === 1) {
            const safe = escapeHtml(seg);
            return `<a href="${safe}" target="_blank" rel="noopener noreferrer" class="text-blue-600 underline hover:text-blue-800 break-all">${safe}</a>`;
        }
        return escapeHtml(seg);
    }).join('');
}

// NOTA v2.0: showSyncingState() eliminado - MongoDB proporciona datos en tiempo real
// Ya no es necesario mostrar "Sincronizando con HubSpot..." porque MongoDB es instantaneo (~5ms)

// =========================================================================
// FUNCIONES DE EDICION DE NOMBRE
// =========================================================================

function openEditNameModal() {
    if (!currentContactId) {
        alert('Selecciona un contacto primero');
        return;
    }

    const currentName = document.getElementById('contactName').textContent;
    const nameParts = currentName.split(' ');
    const firstname = nameParts[0] || '';
    const lastname = nameParts.slice(1).join(' ') || '';

    // Crear modal si no existe
    let modal = document.getElementById('editNameModal');
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'editNameModal';
        modal.className = 'fixed inset-0 bg-black bg-opacity-50 z-50 flex items-center justify-center hidden';
        modal.innerHTML = `
            <div class="bg-white rounded-lg shadow-xl w-full max-w-md p-6">
                <div class="flex justify-between items-center mb-4">
                    <h3 class="text-lg font-semibold">Editar Nombre</h3>
                    <button onclick="closeEditNameModal()" class="text-gray-500 hover:text-gray-700 text-xl">&times;</button>
                </div>
                <form id="editNameForm" onsubmit="saveNameChange(event)">
                    <div class="mb-4">
                        <label class="block text-sm font-medium mb-1">Nombre</label>
                        <input type="text" id="editFirstname" name="firstname" required
                            class="w-full border rounded px-3 py-2 focus:outline-none focus:ring-2 focus:ring-yellow-400"
                            placeholder="Nombre">
                    </div>
                    <div class="mb-4">
                        <label class="block text-sm font-medium mb-1">Apellido</label>
                        <input type="text" id="editLastname" name="lastname"
                            class="w-full border rounded px-3 py-2 focus:outline-none focus:ring-2 focus:ring-yellow-400"
                            placeholder="Apellido (opcional)">
                    </div>
                    <div class="flex gap-2 justify-end">
                        <button type="button" onclick="closeEditNameModal()" class="px-4 py-2 bg-gray-200 rounded hover:bg-gray-300">
                            Cancelar
                        </button>
                        <button type="submit" class="px-4 py-2 bg-[#F5C400] text-[#1A1A1A] font-semibold rounded hover:bg-[#D4A800]">
                            Guardar
                        </button>
                    </div>
                </form>
            </div>
        `;
        document.body.appendChild(modal);
    }

    // Rellenar campos
    document.getElementById('editFirstname').value = firstname;
    document.getElementById('editLastname').value = lastname;

    // Mostrar modal
    modal.classList.remove('hidden');
}

function closeEditNameModal() {
    const modal = document.getElementById('editNameModal');
    if (modal) modal.classList.add('hidden');
}

// =========================================================================
// FUNCION PARA CERRAR CONVERSACION
// =========================================================================

// Limpieza compartida tras un cierre exitoso (botón "Cerrar" o auto-cierre por
// embudo "No responde"). Si el contacto cerrado es el actualmente seleccionado,
// resetea toda la UI del chat. Siempre lo retira del listado optimistamente.
function _performCloseCleanup(closedPhone) {
    if (!closedPhone) return;

    const isCurrent = currentPhone === closedPhone;

    if (isCurrent) {
        currentContactId = null;
        currentPhone = null;
        currentCanal = null;
        currentName = null;

        document.getElementById('contactName').textContent = 'Selecciona un contacto';
        document.getElementById('contactPhone').textContent = '';
        document.getElementById('chatMessages').innerHTML = `
            <div class="flex items-center justify-center h-full text-gray-500">
                <p>Conversacion cerrada. Selecciona otro contacto.</p>
            </div>
        `;
        document.getElementById('messageInput').disabled = true;
        document.getElementById('sendBtn').disabled = true;
        document.getElementById('editNameBtn').classList.add('hidden');
        document.getElementById('closeConversationBtn').classList.add('hidden');
        document.getElementById('transferContactBtn').classList.add('hidden');
        document.getElementById('detailsPanelToggle')?.classList.add('hidden');
        document.getElementById('mainGrid')?.classList.remove('details-hidden');
        const _banner = document.getElementById('handoffBanner');
        if (_banner) _banner.classList.add('hidden');
        _updateDetailsPanel(null);
    }

    // Remoción optimista del listado — el siguiente polling/WS no lo re-muestra
    recentlyClosedPhones.add(closedPhone);
    setTimeout(() => recentlyClosedPhones.delete(closedPhone), 90000);
    try {
        const _cp = JSON.parse(sessionStorage.getItem('_rcpPhones') || '[]');
        _cp.push({ phone: closedPhone, ts: Date.now() });
        sessionStorage.setItem('_rcpPhones', JSON.stringify(_cp));
    } catch (_e) {}
    allContacts = allContacts.filter(c => c.phone !== closedPhone);
    _contactFingerprints.delete(closedPhone);
    _applyFiltersAndRender();
}

async function closeConversation() {
    if (!currentPhone) {
        alert('No hay contacto seleccionado');
        return;
    }

    const confirmMsg = 'Cerrar esta conversacion?\n\n' +
        '- El contacto desaparecera del panel\n' +
        '- Sofia se reactivara para este contacto\n' +
        '- El historial se mantiene en HubSpot';

    if (!confirm(confirmMsg)) {
        return;
    }

    const closeBtn = document.getElementById('closeConversationBtn');
    if (closeBtn) {
        closeBtn.disabled = true;
        closeBtn.textContent = 'Cerrando...';
    }

    try {
        // Construir URL con parametro canal si existe
        let closeUrl = `${BASE_URL}/contacts/${encodeURIComponent(currentPhone)}/close`;
        if (currentCanal) {
            closeUrl += `?canal=${encodeURIComponent(currentCanal)}`;
        }

        const response = await fetch(closeUrl, {
            method: 'DELETE',
            headers: { 'X-API-Key': API_KEY }
        });

        const data = await response.json();

        if (response.ok) {
            _performCloseCleanup(currentPhone);
            alert('Conversacion cerrada correctamente');
        } else {
            throw new Error(data.detail || 'Error cerrando conversacion');
        }

    } catch (error) {
        console.error('[Panel] Error cerrando conversacion:', error);
        alert('Error: ' + error.message);
    } finally {
        if (closeBtn) {
            closeBtn.disabled = false;
            closeBtn.textContent = '&#10005; Cerrar';
        }
    }
}

async function saveNameChange(event) {
    event.preventDefault();

    const firstname = document.getElementById('editFirstname').value.trim();
    const lastname = document.getElementById('editLastname').value.trim();

    if (!firstname) {
        alert('El nombre es obligatorio');
        return;
    }

    try {
        const formData = new FormData();
        formData.append('firstname', firstname);
        formData.append('lastname', lastname);

        const response = await fetch(`${BASE_URL}/contacts/${currentContactId}/name`, {
            method: 'PATCH',
            headers: { 'X-API-Key': API_KEY },
            body: formData
        });

        const data = await response.json();

        if (response.ok) {
            const displayName = data.display_name || `${firstname} ${lastname}`.trim();
            // Actualizar header inmediatamente
            document.getElementById('contactName').textContent = displayName;
            // Actualizar allContacts en memoria → re-render inmediato del sidebar
            const idx = allContacts.findIndex(
                c => (c.contact_id || c.id) === currentContactId
            );
            if (idx !== -1) {
                allContacts[idx].display_name = displayName;
                _applyFiltersAndRender();
            }
            closeEditNameModal();
            // NO llamar loadContacts() inmediatamente: HubSpot tarda ~1-3s en propagar
            // el PATCH internamente. Si se llama ahora, el batch devuelve el nombre viejo
            // y sobreescribe la actualización en memoria. El próximo poll (10s) sincroniza.
            alert('Nombre actualizado correctamente');
        } else {
            throw new Error(data.detail || 'Error actualizando nombre');
        }

    } catch (error) {
        console.error('[Panel] Error actualizando nombre:', error);
        alert('Error: ' + error.message);
    }
}

// =========================================================================
// FUNCIONES DE MULTIMEDIA
// =========================================================================

function handleFileSelect(input) {
    const file = input.files[0];
    if (!file) return;

    // Validar tipo de archivo
    const validTypes = [
        'image/jpeg', 'image/png', 'image/gif', 'image/webp',
        'audio/mpeg', 'audio/wav', 'audio/ogg', 'audio/webm',
        'video/mp4', 'video/quicktime', 'video/3gpp', 'video/webm',
        'application/pdf',
        'application/msword',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'application/vnd.ms-excel',
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    ];
    if (!validTypes.includes(file.type)) {
        alert('Tipo de archivo no soportado. Formatos permitidos: imágenes, audios, videos, PDF, Word, Excel.');
        input.value = '';
        return;
    }

    // Validar tamaño: documentos max 10MB, videos y otros max 16MB
    const isDocument = file.type.includes('pdf') || file.type.includes('word') || file.type.includes('excel') || file.type.includes('spreadsheet');
    const isVideo = file.type.startsWith('video/');
    const maxSize = isDocument ? 10 * 1024 * 1024 : 16 * 1024 * 1024;
    if (file.size > maxSize) {
        if (isDocument) alert('El documento es demasiado grande. Máximo 10MB.');
        else if (isVideo) alert('El video es demasiado grande. Máximo 16MB.');
        else alert('El archivo es demasiado grande. Máximo 16MB.');
        input.value = '';
        return;
    }

    selectedMediaFile = file;

    // Mostrar preview
    const preview = document.getElementById('mediaPreview');
    const previewName = document.getElementById('mediaPreviewName');

    const icon = file.type.startsWith('image/') ? '&#128247;' : (file.type.startsWith('video/') ? '&#127909;' : '&#127911;');
    previewName.innerHTML = `${icon} ${file.name}`;
    preview.classList.remove('hidden');

    console.log('[Panel] Archivo seleccionado:', file.name, file.type, file.size);
}

function clearMediaSelection() {
    selectedMediaFile = null;
    document.getElementById('mediaInput').value = '';
    document.getElementById('mediaPreview').classList.add('hidden');
    console.log('[Panel] Seleccion de archivo limpiada');
}

// =========================================================================
// REPLY / CITAR MENSAJE
// =========================================================================

function selectReplyMessage(msgId) {
    const bubble = document.querySelector(`[data-msg-id="${msgId}"]`);
    if (!bubble) return;

    replyToMessage = {
        id: msgId,
        sender: bubble.dataset.sender || '',
        sender_name: bubble.dataset.senderName || '',
        content: (bubble.dataset.content || '').substring(0, 200),
        media_type: bubble.dataset.mediaType || null,
        timestamp: bubble.dataset.timestamp || ''
    };

    const preview = document.getElementById('replyPreview');
    document.getElementById('replyPreviewSender').textContent = replyToMessage.sender_name || replyToMessage.sender;

    let previewText = replyToMessage.content.substring(0, 80);
    if (replyToMessage.media_type && !replyToMessage.content) {
        previewText = { image: '📷 Imagen', audio: '🎵 Audio', video: '🎬 Video', document: '📄 Documento' }[replyToMessage.media_type] || '📎 Archivo';
    } else if (replyToMessage.media_type) {
        const icon = { image: '📷', audio: '🎵', video: '🎬', document: '📄' }[replyToMessage.media_type] || '📎';
        previewText = icon + ' ' + previewText;
    }
    document.getElementById('replyPreviewContent').textContent = previewText;
    preview.classList.remove('hidden');
    document.getElementById('messageInput').focus();
}

function cancelReply() {
    replyToMessage = null;
    document.getElementById('replyPreview').classList.add('hidden');
}

// =========================================================================
// MENÚ EDITAR / ELIMINAR — mensajes propios del asesor
// =========================================================================

function _closeAllMsgMenus() {
    document.querySelectorAll('.msg-menu-popup').forEach(el => el.remove());
}

document.addEventListener('click', (e) => {
    if (!e.target.closest('.msg-menu-popup') && !e.target.closest('.msg-menu-btn')) {
        _closeAllMsgMenus();
    }
});

function toggleMsgMenu(msgId, btnEl) {
    const existing = document.querySelector(`.msg-menu-popup[data-for="${msgId}"]`);
    _closeAllMsgMenus();
    if (existing) return;

    const popup = document.createElement('div');
    popup.className = 'msg-menu-popup';
    popup.setAttribute('data-for', msgId);
    popup.style.cssText = 'position:absolute;top:24px;right:6px;background:#fff;border:1px solid #e5e7eb;border-radius:8px;box-shadow:0 4px 12px rgba(0,0,0,.1);z-index:50;min-width:140px;overflow:hidden;';
    popup.innerHTML = `
        <button onclick="event.stopPropagation();_closeAllMsgMenus();startEditMessage('${msgId}')"
                style="display:block;width:100%;text-align:left;padding:8px 12px;background:transparent;border:none;cursor:pointer;font-size:13px;color:#374151;"
                onmouseover="this.style.background='#f3f4f6'" onmouseout="this.style.background='transparent'">
            ✏️ Editar
        </button>
        <button onclick="event.stopPropagation();_closeAllMsgMenus();confirmDeleteMessage('${msgId}')"
                style="display:block;width:100%;text-align:left;padding:8px 12px;background:transparent;border:none;cursor:pointer;font-size:13px;color:#dc2626;"
                onmouseover="this.style.background='#fee2e2'" onmouseout="this.style.background='transparent'">
            🗑️ Eliminar
        </button>
    `;
    btnEl.parentElement.appendChild(popup);
}

function _getAdvisorIdQS() {
    return (typeof currentAdvisorId !== 'undefined' && currentAdvisorId)
        ? currentAdvisorId
        : (new URLSearchParams(window.location.search)).get('advisor_id') || '';
}

function startEditMessage(msgId) {
    const bubble = document.querySelector(`[data-msg-id="${msgId}"]`);
    if (!bubble) return;
    const bodyP = bubble.querySelector('p[data-msg-body]');
    if (!bodyP) return;

    const currentText = bubble.dataset.content || bodyP.textContent || '';
    const editorHtml = `
        <div class="edit-msg-wrap" style="margin-top:4px;">
            <textarea class="edit-msg-input" rows="2"
                      style="width:100%;border:1px solid #d1d5db;border-radius:6px;padding:6px;font-size:14px;resize:vertical;">${escapeHtml(currentText)}</textarea>
            <div style="display:flex;gap:6px;justify-content:flex-end;margin-top:4px;">
                <button onclick="cancelEditMessage('${msgId}')"
                        style="padding:4px 10px;font-size:12px;border:1px solid #d1d5db;border-radius:4px;background:#fff;cursor:pointer;">Cancelar</button>
                <button onclick="submitEditMessage('${msgId}', this)"
                        style="padding:4px 10px;font-size:12px;border:none;border-radius:4px;background:#2563eb;color:#fff;cursor:pointer;">Guardar</button>
            </div>
        </div>`;
    bodyP.style.display = 'none';
    bodyP.insertAdjacentHTML('afterend', editorHtml);
    bubble.querySelector('.edit-msg-input').focus();
}

function cancelEditMessage(msgId) {
    const bubble = document.querySelector(`[data-msg-id="${msgId}"]`);
    if (!bubble) return;
    const wrap = bubble.querySelector('.edit-msg-wrap');
    if (wrap) wrap.remove();
    const bodyP = bubble.querySelector('p[data-msg-body]');
    if (bodyP) bodyP.style.display = '';
}

async function submitEditMessage(msgId, btn) {
    const bubble = document.querySelector(`[data-msg-id="${msgId}"]`);
    if (!bubble) return;
    const ta = bubble.querySelector('.edit-msg-input');
    const newContent = (ta?.value || '').trim();
    if (!newContent) { alert('El contenido no puede estar vacío'); return; }

    btn.disabled = true; btn.textContent = 'Guardando...';
    try {
        const advisorId = _getAdvisorIdQS();
        const fd = new FormData();
        fd.append('new_content', newContent);
        fd.append('notify_client', 'true');
        const resp = await fetch(`/whatsapp/panel/messages/${encodeURIComponent(msgId)}?advisor_id=${encodeURIComponent(advisorId)}`, {
            method: 'PATCH',
            body: fd,
        });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.detail || `HTTP ${resp.status}`);
        }
        // Cambio optimista: mostrar nuevo contenido + etiqueta "editado HH:MM"
        const bodyP = bubble.querySelector('p[data-msg-body]');
        if (bodyP) {
            bodyP.textContent = newContent;
            bodyP.style.display = '';
        }
        bubble.dataset.content = newContent;
        const timeP = bubble.querySelector('p.text-xs.text-gray-500.text-right');
        if (timeP) {
            timeP.querySelector('.edit-tag')?.remove();
            const now = new Date();
            const hhmm = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
            timeP.insertAdjacentHTML('beforeend', `<span class="edit-tag text-xs text-gray-400 ml-1">editado ${hhmm}</span>`);
        }
        cancelEditMessage(msgId);
    } catch (e) {
        alert('Error editando mensaje: ' + e.message);
        btn.disabled = false; btn.textContent = 'Guardar';
    }
}

async function confirmDeleteMessage(msgId) {
    if (!confirm('¿Eliminar este mensaje? Se enviará una notificación al cliente.')) return;
    try {
        await _executeDeleteMessage(msgId, true);
    } catch (e) {
        alert('Error eliminando mensaje: ' + e.message);
    }
}

async function _executeDeleteMessage(msgId, notifyClient) {
    const advisorId = _getAdvisorIdQS();
    const qs = new URLSearchParams({
        advisor_id: advisorId,
        notify_client: notifyClient ? 'true' : 'false',
    }).toString();
    const resp = await fetch(`/whatsapp/panel/messages/${encodeURIComponent(msgId)}?${qs}`, {
        method: 'DELETE',
    });
    if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${resp.status}`);
    }
    // Cambio optimista local. El WS también disparará message_deleted.
    const bubble = document.querySelector(`[data-msg-id="${msgId}"]`);
    if (bubble) {
        const isRight = bubble.classList.contains('justify-end');
        bubble.outerHTML = `<div class="flex ${isRight ? 'justify-end' : 'justify-start'} mb-3" data-msg-id="${msgId}"><div class="bubble-deleted p-3 shadow-sm" style="background:#f3f4f6;border-radius:12px;max-width:70%;"><p class="text-xs text-gray-400 italic">Mensaje eliminado</p></div></div>`;
    }
}

function scrollToMessage(msgId) {
    const el = document.querySelector(`[data-msg-id="${msgId}"]`);
    if (!el) return;
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    el.style.transition = 'background-color 0.3s';
    el.style.backgroundColor = 'rgba(245, 196, 0, 0.15)';
    setTimeout(() => { el.style.backgroundColor = ''; }, 1500);
}

// =========================================================================
// REPRODUCTOR DE AUDIO PERSONALIZADO
// =========================================================================

/** Instancias Audio activas. { playerId: { audio: Audio, playing: bool } } */
const _audioPlayers = {};

/** Detecta MIME type del audio por extensión de URL. */
function _detectAudioType(url) {
    if (!url) return 'audio/mpeg';
    if (url.includes('.webm')) return 'audio/webm';
    if (url.includes('.ogg'))  return 'audio/ogg';
    if (url.includes('.mp4') || url.includes('.m4a')) return 'audio/mp4';
    return 'audio/mpeg';
}

/** Alturas en px de las 19 barras del waveform (máx 18px en contenedor 20px). */
const _WAVE_HEIGHTS = [4, 8, 14, 10, 18, 12, 16, 6, 18, 12, 8, 15, 5, 14, 10, 18, 12, 8, 4];

const _PLAY_SVG  = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" width="14" height="14"><path d="M8 5v14l11-7z"/></svg>`;
const _PAUSE_SVG = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" width="14" height="14"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>`;

/**
 * Genera el HTML del reproductor de audio personalizado (WhatsApp-style).
 * @param {string} src       - URL del archivo de audio
 * @param {string} audioType - MIME type ('audio/ogg', 'audio/mpeg', etc.)
 * @param {string} playerId  - ID único para este reproductor
 */
function buildAudioPlayerHtml(src, audioType, playerId) {
    const bars = _WAVE_HEIGHTS
        .map(h => `<span class="audio-bar" style="height:${h}px"></span>`)
        .join('');

    return `<div class="audio-player"
             data-player-id="${playerId}"
             data-src="${src}"
             data-type="${audioType}">
            <button class="audio-play-btn"
                    onclick="toggleAudioPlayer('${playerId}')"
                    title="Reproducir audio"
                    aria-label="Reproducir / Pausar">
                ${_PLAY_SVG}
            </button>
            <div class="audio-waveform-wrap">
                <div class="audio-bars">${bars}</div>
                <div class="audio-progress-track">
                    <div class="audio-progress-fill" id="apf-${playerId}"></div>
                </div>
            </div>
            <span class="audio-duration" id="apd-${playerId}">0:00</span>
        </div>`;
}

/**
 * Alterna reproducción / pausa del reproductor de audio.
 * Pausa automáticamente cualquier otro reproductor activo.
 * @param {string} id - ID del reproductor
 */
function toggleAudioPlayer(id) {
    const el = document.querySelector(`[data-player-id="${id}"]`);
    if (!el) return;

    if (!_audioPlayers[id]) {
        const audio    = new Audio(el.dataset.src);
        const durEl    = document.getElementById(`apd-${id}`);
        const fillEl   = document.getElementById(`apf-${id}`);

        const _fmt = (secs) => {
            const m = Math.floor(secs / 60);
            const s = Math.floor(secs % 60).toString().padStart(2, '0');
            return `${m}:${s}`;
        };

        audio.addEventListener('loadedmetadata', () => {
            if (durEl && isFinite(audio.duration)) durEl.textContent = _fmt(audio.duration);
        });

        audio.addEventListener('timeupdate', () => {
            if (!audio.duration) return;
            if (fillEl) fillEl.style.width = `${(audio.currentTime / audio.duration) * 100}%`;
            if (durEl && isFinite(audio.duration))
                durEl.textContent = _fmt(audio.duration - audio.currentTime);
        });

        audio.addEventListener('ended', () => {
            const p = _audioPlayers[id];
            if (p) p.playing = false;
            el.classList.remove('is-playing');
            const btn = el.querySelector('.audio-play-btn');
            if (btn) btn.innerHTML = _PLAY_SVG;
            if (fillEl) fillEl.style.width = '0%';
            if (durEl && isFinite(audio.duration)) durEl.textContent = _fmt(audio.duration);
            audio.currentTime = 0;
        });

        audio.addEventListener('error', () => {
            console.warn('[AudioPlayer] Error cargando:', el.dataset.src);
        });

        _audioPlayers[id] = { audio, playing: false };
    }

    const player = _audioPlayers[id];
    const btn    = el.querySelector('.audio-play-btn');

    if (player.playing) {
        player.audio.pause();
        player.playing = false;
        el.classList.remove('is-playing');
        if (btn) btn.innerHTML = _PLAY_SVG;
    } else {
        // Pausar todos los demás reproductores activos
        Object.entries(_audioPlayers).forEach(([pid, p]) => {
            if (pid !== id && p.playing) {
                p.audio.pause();
                p.playing = false;
                const other = document.querySelector(`[data-player-id="${pid}"]`);
                if (other) {
                    other.classList.remove('is-playing');
                    const ob = other.querySelector('.audio-play-btn');
                    if (ob) ob.innerHTML = _PLAY_SVG;
                }
            }
        });

        player.audio.play().catch(e => console.warn('[AudioPlayer] Error reproduciendo:', e));
        player.playing = true;
        el.classList.add('is-playing');
        if (btn) btn.innerHTML = _PAUSE_SVG;
    }
}

// =========================================================================
// FUNCIONES DE GRABACION DE AUDIO
// =========================================================================

let mediaRecorder = null;
let audioChunks = [];
let recordingInterval = null;
let recordingStartTime = null;

/**
 * Inicia la grabacion de audio desde el microfono del navegador.
 *
 * IMPORTANTE: WhatsApp requiere formatos especificos para reproducir audio:
 * - audio/ogg (con codec Opus) - PREFERIDO
 * - audio/mp4, audio/aac, audio/mpeg
 * - audio/webm NO es compatible directamente con WhatsApp
 *
 * Estrategia:
 * 1. Intentar grabar en OGG/Opus (Firefox lo soporta nativamente)
 * 2. Si no, grabar en WebM y convertir a WAV antes de enviar
 * 3. El servidor puede procesar WAV correctamente
 */
async function startRecording() {
    console.log('[Panel] Iniciando grabacion de audio...');

    // Verificar soporte del navegador
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        alert('Tu navegador no soporta grabacion de audio. Usa Chrome, Edge o Firefox.');
        return;
    }

    try {
        // Solicitar acceso al microfono con configuracion optima
        const stream = await navigator.mediaDevices.getUserMedia({
            audio: {
                channelCount: 1,        // Mono (reduce tamano)
                sampleRate: 16000,      // 16kHz (suficiente para voz)
                echoCancellation: true,
                noiseSuppression: true
            }
        });
        console.log('[Panel] Acceso al microfono concedido');

        // Determinar el mejor formato soportado para WhatsApp
        // PRIORIDAD: OGG/Opus > MP4 > WebM (convertir a WAV)
        let mimeType = null;
        let needsConversion = false;

        // 1. Intentar OGG/Opus (compatible con WhatsApp, Firefox lo soporta)
        if (MediaRecorder.isTypeSupported('audio/ogg;codecs=opus')) {
            mimeType = 'audio/ogg;codecs=opus';
            console.log('[Panel] Usando OGG/Opus (compatible con WhatsApp)');
        }
        // 2. Intentar MP4/AAC (compatible con WhatsApp)
        else if (MediaRecorder.isTypeSupported('audio/mp4')) {
            mimeType = 'audio/mp4';
            console.log('[Panel] Usando MP4 (compatible con WhatsApp)');
        }
        // 3. Fallback a WebM (NO compatible con WhatsApp, necesita conversion)
        else if (MediaRecorder.isTypeSupported('audio/webm;codecs=opus')) {
            mimeType = 'audio/webm;codecs=opus';
            needsConversion = true;
            console.log('[Panel] Usando WebM (se convertira a WAV para WhatsApp)');
        }
        else if (MediaRecorder.isTypeSupported('audio/webm')) {
            mimeType = 'audio/webm';
            needsConversion = true;
            console.log('[Panel] Usando WebM basico (se convertira a WAV)');
        }
        else {
            throw new Error('Tu navegador no soporta ningun formato de audio compatible');
        }

        // Crear MediaRecorder
        mediaRecorder = new MediaRecorder(stream, { mimeType });
        audioChunks = [];

        // Guardar referencia al stream para cerrarlo despues
        mediaRecorder._stream = stream;
        mediaRecorder._needsConversion = needsConversion;
        mediaRecorder._mimeType = mimeType;

        // Evento: datos disponibles
        mediaRecorder.ondataavailable = (event) => {
            if (event.data.size > 0) {
                audioChunks.push(event.data);
            }
        };

        // Evento: grabacion detenida
        mediaRecorder.onstop = async () => {
            console.log('[Panel] Grabacion detenida, procesando audio...');

            const originalMimeType = mediaRecorder._mimeType;
            const needsConv = mediaRecorder._needsConversion;

            // Crear blob con los chunks originales
            const audioBlob = new Blob(audioChunks, { type: originalMimeType });
            console.log('[Panel] Audio grabado:', audioBlob.size, 'bytes, tipo:', originalMimeType);

            let finalBlob = audioBlob;
            let finalMimeType = originalMimeType;
            let extension = 'ogg';

            // Si es WebM, convertir a WAV para compatibilidad con WhatsApp
            if (needsConv) {
                console.log('[Panel] Convirtiendo WebM a WAV para compatibilidad...');
                try {
                    finalBlob = await convertToWav(audioBlob);
                    finalMimeType = 'audio/wav';
                    extension = 'wav';
                    console.log('[Panel] Conversion exitosa:', finalBlob.size, 'bytes');
                } catch (convErr) {
                    console.error('[Panel] Error en conversion:', convErr);
                    // IMPORTANTE: Actualizar mimeType al original para que el servidor
                    // sepa que es WebM y pueda convertirlo server-side
                    finalMimeType = originalMimeType;
                    extension = 'webm';
                    console.warn('[Panel] Enviando WebM original - servidor debe convertir a OGG');
                }
            } else if (originalMimeType.includes('ogg')) {
                extension = 'ogg';
            } else if (originalMimeType.includes('mp4')) {
                extension = 'mp4';
            }

            // Convertir a File para enviar por FormData
            const audioFile = new File(
                [finalBlob],
                `nota_voz_${Date.now()}.${extension}`,
                { type: finalMimeType }
            );

            // Cerrar stream del microfono
            mediaRecorder._stream.getTracks().forEach(track => track.stop());
            console.log('[Panel] Stream de microfono cerrado');

            // Mostrar confirmacion y enviar
            confirmAndSendAudio(audioFile);
        };

        // Evento: error
        mediaRecorder.onerror = (event) => {
            console.error('[Panel] Error en MediaRecorder:', event.error);
            alert('Error durante la grabacion: ' + event.error.message);
            stopRecording();
        };

        // Iniciar grabacion
        mediaRecorder.start(1000); // Chunks cada 1 segundo
        console.log('[Panel] Grabacion iniciada con formato:', mimeType);

        // Actualizar UI
        updateRecordingUI(true);

    } catch (err) {
        console.error('[Panel] Error al acceder al microfono:', err);

        if (err.name === 'NotAllowedError') {
            alert('Permiso denegado. Por favor permite el acceso al microfono en tu navegador.');
        } else if (err.name === 'NotFoundError') {
            alert('No se encontro ningun microfono. Conecta un microfono e intenta de nuevo.');
        } else {
            alert('Error al acceder al microfono: ' + err.message);
        }
    }
}

/**
 * Convierte un Blob de audio WebM a WAV usando AudioContext.
 * WAV es compatible con WhatsApp y Twilio.
 *
 * @param {Blob} webmBlob - Audio en formato WebM
 * @returns {Promise<Blob>} - Audio en formato WAV
 */
async function convertToWav(webmBlob) {
    // Crear AudioContext
    const audioContext = new (window.AudioContext || window.webkitAudioContext)();

    // Decodificar el audio WebM
    const arrayBuffer = await webmBlob.arrayBuffer();
    const audioBuffer = await audioContext.decodeAudioData(arrayBuffer);

    // Configuracion WAV
    const numChannels = 1; // Mono
    const sampleRate = 16000; // 16kHz para voz
    const bitsPerSample = 16;

    // Resamplear si es necesario
    let samples;
    if (audioBuffer.sampleRate !== sampleRate) {
        // Resamplear a 16kHz
        const offlineContext = new OfflineAudioContext(
            numChannels,
            audioBuffer.duration * sampleRate,
            sampleRate
        );
        const source = offlineContext.createBufferSource();
        source.buffer = audioBuffer;
        source.connect(offlineContext.destination);
        source.start();
        const resampledBuffer = await offlineContext.startRendering();
        samples = resampledBuffer.getChannelData(0);
    } else {
        samples = audioBuffer.getChannelData(0);
    }

    // Convertir float32 a int16
    const int16Samples = new Int16Array(samples.length);
    for (let i = 0; i < samples.length; i++) {
        const s = Math.max(-1, Math.min(1, samples[i]));
        int16Samples[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
    }

    // Crear header WAV
    const wavBuffer = createWavBuffer(int16Samples, sampleRate, numChannels, bitsPerSample);

    // Cerrar AudioContext
    await audioContext.close();

    return new Blob([wavBuffer], { type: 'audio/wav' });
}

/**
 * Crea un buffer WAV completo con header y datos.
 */
function createWavBuffer(samples, sampleRate, numChannels, bitsPerSample) {
    const bytesPerSample = bitsPerSample / 8;
    const blockAlign = numChannels * bytesPerSample;
    const byteRate = sampleRate * blockAlign;
    const dataSize = samples.length * bytesPerSample;
    const bufferSize = 44 + dataSize; // 44 bytes header + data

    const buffer = new ArrayBuffer(bufferSize);
    const view = new DataView(buffer);

    // RIFF header
    writeString(view, 0, 'RIFF');
    view.setUint32(4, bufferSize - 8, true);
    writeString(view, 8, 'WAVE');

    // fmt subchunk
    writeString(view, 12, 'fmt ');
    view.setUint32(16, 16, true); // Subchunk1Size (16 for PCM)
    view.setUint16(20, 1, true);  // AudioFormat (1 = PCM)
    view.setUint16(22, numChannels, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, byteRate, true);
    view.setUint16(32, blockAlign, true);
    view.setUint16(34, bitsPerSample, true);

    // data subchunk
    writeString(view, 36, 'data');
    view.setUint32(40, dataSize, true);

    // Write samples
    const offset = 44;
    for (let i = 0; i < samples.length; i++) {
        view.setInt16(offset + i * 2, samples[i], true);
    }

    return buffer;
}

/**
 * Escribe un string en un DataView.
 */
function writeString(view, offset, string) {
    for (let i = 0; i < string.length; i++) {
        view.setUint8(offset + i, string.charCodeAt(i));
    }
}

/**
 * Detiene la grabacion actual.
 */
function stopRecording() {
    console.log('[Panel] Deteniendo grabacion...');

    if (mediaRecorder && mediaRecorder.state !== 'inactive') {
        mediaRecorder.stop();
    }

    updateRecordingUI(false);
}

/**
 * Muestra confirmacion y envia el audio grabado.
 * @param {File} audioFile - Archivo de audio grabado
 */
function confirmAndSendAudio(audioFile) {
    // Mostrar preview del audio antes de enviar
    const preview = document.getElementById('mediaPreview');
    const previewName = document.getElementById('mediaPreviewName');

    previewName.innerHTML = `&#127911; ${audioFile.name} (${(audioFile.size / 1024).toFixed(1)} KB)`;
    preview.classList.remove('hidden');

    // Setear el archivo para que sendMessage lo use
    selectedMediaFile = audioFile;

    console.log('[Panel] Audio listo para enviar:', audioFile.name, audioFile.type, audioFile.size);

    // Aviso: WhatsApp no permite caption en audio. Si hay cita activa, irá como mensaje de texto previo.
    if (replyToMessage) {
        showToast('Tu cita aparecerá como mensaje de texto antes del audio.', 'info');
    }

    // Preguntar si desea enviar inmediatamente o agregar texto
    const sendNow = confirm('Audio grabado. ¿Deseas enviarlo ahora?\n\nPresiona "Cancelar" para agregar un mensaje de texto antes de enviar.');

    if (sendNow) {
        // Enviar inmediatamente
        sendMessage(new Event('submit'));
    }
    // Si cancela, el archivo queda en selectedMediaFile y puede agregar texto
}

/**
 * Actualiza la UI segun el estado de grabacion.
 * @param {boolean} isRecording - Si esta grabando o no
 */
function updateRecordingUI(isRecording) {
    const recordBtn = document.getElementById('recordBtn');
    const recordingStatus = document.getElementById('recordingStatus');
    const timerDisplay = document.getElementById('recordTimer');
    const attachBtn = document.getElementById('attachBtn');
    const messageInput = document.getElementById('messageInput');
    const sendBtn = document.getElementById('sendBtn');

    if (isRecording) {
        // Ocultar boton de microfono, mostrar indicador de grabacion
        recordBtn.classList.add('hidden');
        recordingStatus.classList.remove('hidden');

        // Deshabilitar otros controles mientras graba
        attachBtn.disabled = true;
        messageInput.disabled = true;
        sendBtn.disabled = true;

        // Iniciar contador de tiempo
        recordingStartTime = Date.now();
        recordingInterval = setInterval(() => {
            const elapsed = Math.floor((Date.now() - recordingStartTime) / 1000);
            const minutes = Math.floor(elapsed / 60).toString().padStart(2, '0');
            const seconds = (elapsed % 60).toString().padStart(2, '0');
            timerDisplay.textContent = `${minutes}:${seconds}`;
        }, 1000);

        console.log('[Panel] UI actualizada: grabando');

    } else {
        // Mostrar boton de microfono, ocultar indicador
        recordBtn.classList.remove('hidden');
        recordingStatus.classList.add('hidden');

        // Rehabilitar controles
        attachBtn.disabled = false;
        messageInput.disabled = false;
        sendBtn.disabled = false;

        // Detener contador
        if (recordingInterval) {
            clearInterval(recordingInterval);
            recordingInterval = null;
        }

        // Resetear timer display
        if (timerDisplay) {
            timerDisplay.textContent = '00:00';
        }

        console.log('[Panel] UI actualizada: no grabando');
    }
}

/**
 * Cancela la grabacion actual sin enviar.
 */
function cancelRecording() {
    console.log('[Panel] Cancelando grabacion...');

    if (mediaRecorder && mediaRecorder.state !== 'inactive') {
        // Remover el handler de onstop para que no intente enviar
        mediaRecorder.onstop = () => {
            console.log('[Panel] Grabacion cancelada');
        };
        mediaRecorder.stop();
    }

    audioChunks = [];
    updateRecordingUI(false);
}

// =========================================================================
// URL ROUTING — sincronización SPA de la URL con el contacto abierto
// =========================================================================

/**
 * Actualiza el parámetro `phone` de la URL sin recargar la página.
 * Preserva todos los demás params (key=, advisor=, etc.).
 * null → elimina el param (estado vacío del panel).
 */
function _syncUrlToContact(phone) {
    const p = new URLSearchParams(window.location.search);
    if (phone) {
        p.set('phone', phone);
    } else {
        p.delete('phone');
    }
    history.replaceState({ phone: phone || null }, '', `?${p.toString()}`);
}

/**
 * Deselecciona el contacto activo y restaura el panel al estado vacío.
 * Actualiza la URL eliminando el param phone=.
 */
function deselectContact() {
    if (!currentContactId && !currentPhone) return;
    currentContactId = null;
    currentPhone = null;
    currentCanal = null;
    currentName = null;
    isFirstChatLoad = true;
    renderedContactId = null;

    _syncUrlToContact(null);

    // Deseleccionar visual en la lista
    document.querySelectorAll('.contact-item.active').forEach(el => el.classList.remove('active'));

    // Restaurar encabezado
    const nameEl = document.getElementById('contactName');
    if (nameEl) nameEl.textContent = 'Selecciona un contacto';
    const phoneEl = document.getElementById('contactPhone');
    if (phoneEl) phoneEl.textContent = '';
    document.getElementById('canalDisplayWrapper')?.classList.add('hidden');

    // Restaurar área central vacía
    const chatMessages = document.getElementById('chatMessages');
    if (chatMessages) chatMessages.innerHTML = '';

    // Deshabilitar input y botones
    const msgInput = document.getElementById('messageInput');
    if (msgInput) msgInput.disabled = true;
    const sendBtn = document.getElementById('sendBtn');
    if (sendBtn) sendBtn.disabled = true;
    document.getElementById('editNameBtn')?.classList.add('hidden');
    document.getElementById('closeConversationBtn')?.classList.add('hidden');
    document.getElementById('transferContactBtn')?.classList.add('hidden');
    document.getElementById('detailsPanelToggle')?.classList.add('hidden');
    document.getElementById('mainGrid')?.classList.remove('details-hidden');
    const banner = document.getElementById('handoffBanner');
    if (banner) banner.classList.add('hidden');

    _updateDetailsPanel(null);
}

// =========================================================================
// FUNCIONES DE INTERACCION
// =========================================================================

async function selectContact(contactId, phone, displayName, canal = null) {
    // Guard: phone es requerido — selectContact sin phone crea estado "ghost"
    // (header visible pero handlers de editar/cerrar/plantilla fallan con
    // "debes seleccionar un contacto primero"). Abortamos explícitamente.
    if (!phone) {
        console.error('[Panel] selectContact llamado sin phone — abortando para evitar ghost state', { contactId, displayName, canal });
        try { showToast && showToast('No se pudo abrir el contacto (datos incompletos)', 'error'); } catch (_e) {}
        return;
    }

    // Si es el mismo contacto, no hacer nada (evita recargas innecesarias)
    if (currentContactId === contactId && currentPhone === phone) {
        console.log('[Panel] Mismo contacto seleccionado, ignorando');
        return;
    }


    // Limpiar "marcado como no leído" manual al abrir el contacto
    if (phone && _manuallyUnread.has(phone)) {
        _manuallyUnread.delete(phone);
        _saveManuallyUnread();
        const manualBadge = document.querySelector(`.contact-item[data-phone="${CSS.escape(phone)}"] .manual-unread-badge`);
        if (manualBadge) manualBadge.remove();
    }

    // Al abrir el chat de un contacto, marcar sus mensajes como leídos
    if (phone && unreadCounts[phone]) {
        delete unreadCounts[phone];
        // Re-renderizar solo el ítem de la lista para quitar el badge
        const phoneToReset = phone;
        const contactItem = document.querySelector(`.contact-item[data-phone="${phoneToReset}"]`);
        if (contactItem) {
            const badge = contactItem.querySelector('.unread-badge');
            if (badge) badge.remove();
        }
    }

    // Inbox persistente: registrar como visto en esta sesión + marcar leído en Redis.
    // Siempre se dispara: si ADVISOR_ID está disponible se envía, si no el backend
    // lo infiere de ConversationMeta.assigned_owner_id (no más 422 por param faltante).
    if (phone) {
        _seenPhones.add(phone);
        const _mrParams = new URLSearchParams();
        if (ADVISOR_ID) _mrParams.set('advisor_id', ADVISOR_ID);
        if (canal) _mrParams.set('canal', canal);
        const _mrQuery = _mrParams.toString() ? `?${_mrParams.toString()}` : '';
        fetch(`${BASE_URL}/contacts/${encodeURIComponent(phone)}/mark-read${_mrQuery}`, {
            method: 'POST',
            headers: { 'X-API-Key': API_KEY }
        }).catch(err => console.warn('[Panel][Inbox] mark-read error (non-fatal):', err));
    }

    // Mostrar toggle de Columna C y asegurar que el panel esté visible
    document.getElementById('detailsPanelToggle')?.classList.remove('hidden');
    document.getElementById('mainGrid')?.classList.remove('details-hidden');

    currentContactId = contactId;
    currentPhone = phone;
    currentCanal = canal;  // Guardar canal para segregacion
    currentName = displayName || null;

    // Sincronizar URL para que F5 restaure este contacto
    _syncUrlToContact(phone);

    // Limpiar estado del template picker y reply al cambiar de contacto
    activeTemplateId = null; activeTemplateBody = ''; activeTemplateVars = [];
    closeTemplatePicker();
    cancelReply();
    const hint = document.getElementById('templateHint');
    if (hint) hint.classList.add('hidden');

    // Reiniciar polling con intervalo mas rapido para chat activo
    restartPollingForChat();

    // Resetear estado de primera carga para nuevo contacto
    isFirstChatLoad = true;

    // Mostrar indicador de carga sobre el chat existente (no limpiar por completo)
    const chatContainer = document.getElementById('chatMessages');
    if (chatContainer) {
        // Añadir overlay de carga sin destruir el contenido
        const existingOverlay = chatContainer.querySelector('.loading-overlay');
        if (!existingOverlay) {
            const overlay = document.createElement('div');
            overlay.className = 'loading-overlay absolute inset-0 bg-white bg-opacity-80 flex items-center justify-center z-10';
            overlay.innerHTML = '<p class="text-gray-500">Cargando historial...</p>';
            chatContainer.style.position = 'relative';
            chatContainer.appendChild(overlay);
        }
    }

    // Actualizar header
    document.getElementById('contactName').textContent = displayName;
    document.getElementById('contactPhone').textContent = phone;

    // Mostrar boton de editar nombre
    const editBtn = document.getElementById('editNameBtn');
    if (editBtn) editBtn.classList.remove('hidden');

    // Mostrar boton de cerrar conversacion
    const closeBtn = document.getElementById('closeConversationBtn');
    if (closeBtn) closeBtn.classList.remove('hidden');

    // Mostrar boton de transferir contacto
    const transferBtn = document.getElementById('transferContactBtn');
    if (transferBtn) transferBtn.classList.remove('hidden');

    // Habilitar inputs (loadContactDetail ajustará si la ventana está cerrada)
    document.getElementById('messageInput').disabled = false;
    document.getElementById('sendBtn').disabled = false;
    document.getElementById('attachBtn').disabled = false;
    document.getElementById('recordBtn').disabled = false;
    // Resetear estado de ventana cerrada (se actualizará en loadContactDetail)
    document.getElementById('templateTriggerBtn')?.classList.add('hidden');
    document.getElementById('attachBtn')?.classList.remove('hidden');
    document.getElementById('recordBtn')?.classList.remove('hidden');
    currentWindowOpen = true;
    document.getElementById('selectedPhone').value = phone;
    document.getElementById('selectedContactId').value = contactId;

    // Limpiar cualquier archivo multimedia previo
    clearMediaSelection();

    // Actualizar visualmente la seleccion en la lista (inmediatamente)
    // Usamos data-phone (atributo ya presente en el DOM) en lugar de parsear el string
    // del onclick — evita fallas cuando contact_id está vacío o contiene caracteres raros.
    document.querySelectorAll('.contact-item').forEach(el => {
        el.classList.remove('active');
    });
    const selectedItem = phone
        ? document.querySelector(`.contact-item[data-phone="${CSS.escape(phone)}"]`)
        : null;
    if (selectedItem) {
        selectedItem.classList.add('active');
    }

    // =========================================================================
    // OPTIMIZACION: Ejecutar take-control y loadChatHistory en paralelo
    // Esto reduce el tiempo total de carga al seleccionar un contacto
    // =========================================================================
    const takeControlPromise = (async () => {
        try {
            const takeControlUrl = `${BASE_URL}/contacts/${encodeURIComponent(phone)}/take-control?` +
                `canal=${encodeURIComponent(canal || 'whatsapp')}` +
                `&contact_id=${encodeURIComponent(contactId || '')}` +
                (ADVISOR_ID ? `&advisor_id=${encodeURIComponent(ADVISOR_ID)}` : '');

            const response = await fetch(takeControlUrl, {
                method: 'POST',
                headers: { 'X-API-Key': API_KEY }
            });

            const data = await response.json();
            console.log('[Panel] Take Control response:', data);

            if (data.status === 'success') {
                console.log(`[Panel] Control tomado: ${data.action} - Sofia pausada`);
            }
        } catch (error) {
            console.warn('[Panel] Error en take-control (no critico):', error);
        }
    })();

    // Endpoint combinado: historial + window-status en 1 solo round-trip
    const loadDetailPromise = phone
        ? loadContactDetail(phone, contactId, canal)
        : loadChatHistory(contactId);

    // Esperar a que todas las operaciones completen
    await Promise.all([takeControlPromise, loadDetailPromise]);

    // Remover overlay de carga
    if (chatContainer) {
        const overlay = chatContainer.querySelector('.loading-overlay');
        if (overlay) overlay.remove();
    }

    // Actualizar banner de handoff y panel de detalles con datos del contacto seleccionado
    const _selectedContact = allContacts.find(c => c.phone === phone || (c.contact_id || c.id) === contactId);
    if (_selectedContact) {
        _updateHandoffBanner(_selectedContact.conversation_status || _selectedContact.status, displayName);
        _updateDetailsPanel(_selectedContact);
    } else {
        _updateHandoffBanner('BOT_ACTIVE', displayName);
    }
}

// =========================================================================
// BANNER DE HANDOFF — Indicador de modo Sofía vs Asesora
// =========================================================================

/**
 * Actualiza el banner de handoff según el estado de la conversación.
 * @param {string} status - BOT_ACTIVE | HUMAN_ACTIVE | IN_CONVERSATION | PENDING_HANDOFF
 * @param {string} contactName - Nombre del contacto seleccionado
 */
function _updateHandoffBanner(status, contactName) {
    const banner = document.getElementById('handoffBanner');
    const icon = document.getElementById('handoffBannerIcon');
    const text = document.getElementById('handoffBannerText');
    const takeBtn = document.getElementById('handoffTakeControlBtn');
    if (!banner) return;

    banner.classList.remove('hidden', 'handoff-banner-bot', 'handoff-banner-human',
                            'handoff-banner-pending', 'handoff-banner-in-conversation');

    const name = contactName || 'el contacto';

    if (status === 'PENDING_HANDOFF') {
        banner.classList.add('handoff-banner-pending');
        icon.textContent = '🔔';
        text.textContent = `${name} necesita atención urgente`;
        takeBtn.classList.remove('hidden');
    } else if (status === 'HUMAN_ACTIVE') {
        banner.classList.add('handoff-banner-human');
        icon.textContent = '👤';
        text.textContent = `Es tu turno — ${name} está esperando`;
        takeBtn.classList.add('hidden');
    } else if (status === 'IN_CONVERSATION') {
        banner.classList.add('handoff-banner-in-conversation');
        icon.textContent = '💬';
        text.textContent = `Conversación activa con ${name}`;
        takeBtn.classList.add('hidden');
    } else {
        // BOT_ACTIVE o desconocido
        banner.classList.add('handoff-banner-bot');
        icon.textContent = '🤖';
        text.textContent = 'Sofía está atendiendo en piloto automático';
        takeBtn.classList.remove('hidden');
    }
}

/**
 * Toma control desde el botón del banner (cuando Sofía está activa).
 * Reutiliza el endpoint de take-control ya definido.
 */
async function takeControlFromBanner() {
    if (!currentPhone) return;
    const takeControlUrl = `${BASE_URL}/contacts/${encodeURIComponent(currentPhone)}/take-control?` +
        `canal=${encodeURIComponent(currentCanal || 'whatsapp')}` +
        (ADVISOR_ID ? `&advisor_id=${encodeURIComponent(ADVISOR_ID)}` : '');
    try {
        await fetch(takeControlUrl, { method: 'POST', headers: { 'X-API-Key': API_KEY } });
        scheduleContactsRefresh();
    } catch (e) {
        console.warn('[Panel] Error tomar control desde banner:', e);
    }
}

/**
 * Muestra/oculta la Columna C (panel de detalles).
 * En pantallas <1280px la columna es un drawer fixed; en ≥1280px se alterna la visibilidad.
 */
function toggleDetailsPanel() {
    const grid = document.getElementById('mainGrid');
    if (!grid) return;
    if (window.innerWidth < 1280) {
        // En móvil: drawer fixed
        const panel = document.getElementById('detailsPanel');
        if (panel) panel.classList.toggle('panel-open');
    } else {
        // En desktop: colapsar columna del grid
        grid.classList.toggle('details-hidden');
    }
}

// =========================================================================
// PANEL DE DETALLES (COLUMNA C) — Lead info, datos básicos
// =========================================================================
//kjiuhiu
/**
 * Popula la columna C con los datos del contacto seleccionado.
 * Usa los datos ya disponibles en allContacts (sin fetch adicional).
 * @param {Object} contact - Objeto de contacto de allContacts
 */
function _updateDetailsPanel(contact) {
    const empty = document.getElementById('detailsEmpty');
    const content = document.getElementById('detailsContent');
    const scheduleBtn = document.getElementById('scheduleApptPanelBtn');
    if (!content) return;

    if (!contact) {
        if (empty) empty.classList.remove('hidden');
        content.classList.add('hidden');
        return;
    }

    if (empty) empty.classList.add('hidden');
    content.classList.remove('hidden');

    // Mostrar botón de agendar solo si hay contactId
    if (scheduleBtn) {
        scheduleBtn.classList.toggle('hidden', !contact.contact_id);
    }

    // Cargar próxima cita, mensaje programado y notas de forma asíncrona
    if (contact.contact_id) {
        loadNextAppointmentForPanel(contact.contact_id);
        loadScheduledMessageForPanel(contact.contact_id);
        loadNotes(contact.contact_id);
    } else {
        const apptEl = document.getElementById('nextAppointmentContent');
        if (apptEl) apptEl.innerHTML = '<p class="text-sm text-gray-400">Sin cita programada</p>';
        const smEl = document.getElementById('scheduledMessageContent');
        if (smEl) smEl.innerHTML = '<p class="text-sm text-gray-400">Sin mensaje programado</p>';
        const notesEl = document.getElementById('notesList');
        if (notesEl) notesEl.innerHTML = '<p class="text-xs text-gray-400 px-1">Sin contactId disponible</p>';
    }

    // Construir lead info
    const infoEl = document.getElementById('leadInfoContent');
    if (!infoEl) return;

    // Stage label desde PIPELINE_STAGES
    const stageLabel = PIPELINE_STAGES.find(s => s.id === contact.current_stage)?.name || contact.current_stage || '—';

    infoEl.innerHTML = `
        <div class="flex items-center justify-between py-1 border-b border-gray-100">
            <span class="text-gray-500 text-xs">Canal</span>
            ${_buildCanalDropdown(contact)}
        </div>
        <div class="flex items-center justify-between py-1 border-b border-gray-100">
            <span class="text-gray-500 text-xs">Etapa</span>
            ${contact.contact_id
                ? _buildPipelineDropdown(contact.contact_id, contact.current_stage)
                : `<span class="text-xs font-semibold text-gray-800">${stageLabel}</span>`}
        </div>
        <div class="flex items-center justify-between py-1 border-b border-gray-100">
            <span class="text-gray-500 text-xs">Teléfono</span>
            <span class="text-xs font-mono text-gray-700">${contact.phone || '—'}</span>
        </div>
        ${contact.owner_name ? `
        <div class="flex items-center justify-between py-1">
            <span class="text-gray-500 text-xs">Asesora</span>
            <span class="text-xs font-medium text-gray-700">${contact.owner_name}</span>
        </div>` : ''}
    `;
}

// =========================================================================
// CITAS EN COLUMNA C — Próxima cita del contacto seleccionado
// =========================================================================

/**
 * Carga la próxima cita del contacto y la renderiza en la Columna C.
 * Usa el endpoint existente GET /contacts/{contact_id}/appointments
 * @param {string} contactId - HubSpot contact ID
 */
async function loadNextAppointmentForPanel(contactId) {
    const container = document.getElementById('nextAppointmentContent');
    const btn = document.getElementById('scheduleApptPanelBtn');
    if (!container || !contactId) return;

    container.innerHTML = '<p class="text-xs text-gray-400 animate-pulse">Cargando...</p>';

    try {
        const resp = await fetch(
            `${BASE_URL}/contacts/${encodeURIComponent(contactId)}/appointments`,
            { headers: { 'X-API-Key': API_KEY } }
        );
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        const appts = data.appointments || data || [];

        // Filtrar solo futuras o pendientes/confirmadas
        const now = new Date();
        const upcoming = (appts || [])
            .filter(a => a.status !== 'cancelled' && new Date(a.appointment_dt) >= now)
            .sort((a, b) => new Date(a.appointment_dt) - new Date(b.appointment_dt));

        if (upcoming.length === 0) {
            container.innerHTML = '<p class="text-sm text-gray-400">Sin cita programada</p>';
            if (btn) btn.classList.remove('hidden');
            return;
        }

        const next = upcoming[0];
        const dt = new Date(next.appointment_dt);
        const dateStr = dt.toLocaleDateString('es-CO', { weekday: 'short', day: 'numeric', month: 'short' });
        const timeStr = dt.toLocaleTimeString('es-CO', { hour: '2-digit', minute: '2-digit', hour12: true });
        const statusColors = { pending: 'text-amber-600', confirmed: 'text-emerald-600', completed: 'text-gray-400' };
        const statusColor = statusColors[next.status] || 'text-gray-500';

        container.innerHTML = `
            <div class="appt-card">
                <div class="flex items-start justify-between">
                    <div>
                        <p class="text-sm font-semibold text-gray-800">📅 ${dateStr}</p>
                        <p class="text-xs text-gray-600 mt-0.5">⏰ ${timeStr}</p>
                        ${next.worker_name ? `<p class="text-xs text-gray-500 mt-0.5">👤 ${next.worker_name}</p>` : ''}
                        ${next.notes ? `<p class="text-xs text-gray-500 mt-1 italic truncate">${next.notes}</p>` : ''}
                    </div>
                    <span class="text-xs font-semibold ${statusColor} capitalize">${next.status}</span>
                </div>
                ${upcoming.length > 1 ? `<p class="text-xs text-gray-400 mt-2">+${upcoming.length - 1} cita(s) más</p>` : ''}
            </div>
        `;

        if (btn) btn.classList.remove('hidden');

    } catch (err) {
        console.warn('[Panel] Error cargando cita para columna C:', err);
        container.innerHTML = '<p class="text-xs text-red-400">Error al cargar cita</p>';
    }
}

// =========================================================================
// NOTAS INTERNAS — Módulo de Notas en Columna C
// =========================================================================

/** Muestra el formulario de nueva nota y pone foco en el textarea. */
function openAddNote() {
    const form = document.getElementById('addNoteForm');
    if (form) {
        form.classList.remove('hidden');
        document.getElementById('newNoteText')?.focus();
    }
}

/** Oculta y limpia el formulario de nueva nota. */
function cancelAddNote() {
    const form = document.getElementById('addNoteForm');
    if (form) {
        form.classList.add('hidden');
        const t = document.getElementById('newNoteText');
        if (t) t.value = '';
    }
}

/**
 * Carga las notas activas del contacto y renderiza en #notesList.
 * @param {string} contactId - HubSpot contact ID
 */
async function loadNotes(contactId) {
    const container = document.getElementById('notesList');
    if (!container || !contactId) return;

    container.innerHTML = '<p class="text-xs text-gray-400 animate-pulse px-1">Cargando notas...</p>';

    try {
        const resp = await fetch(
            `${BASE_URL}/contacts/${encodeURIComponent(contactId)}/notes`,
            { headers: { 'X-API-Key': API_KEY } }
        );
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        renderNotes(data.notes || [], contactId);
    } catch (err) {
        console.warn('[Panel] Error cargando notas:', err);
        container.innerHTML = '<p class="text-xs text-red-400 px-1">Error al cargar notas</p>';
    }
}

// =========================================================================
// MENSAJES PROGRAMADOS — Plantillas WhatsApp en fecha específica
// =========================================================================

const SCHEDULABLE_TEMPLATES = [
    {
        sid: 'HX550a2475d09a5fb3b5410e6d36eadf3f',
        name: 'Aun_interesado',
        label: '¿Aún estás interesado?',
        preview: 'Hola {1} 😊 ¿Aún continúas en la búsqueda de un inmueble para arriendo?',
        vars: [{ key: '1', label: 'Nombre del contacto' }]
    },
    {
        sid: 'HX015a4c21c7aeb082448aeaa97396dbbf',
        name: 'Seguimiento_Personalizado',
        label: 'Seguimiento Personalizado',
        preview: 'Hola {1} ¿Como se encuentra el día de hoy? {2}. Estaré pendiente a su respuesta.',
        vars: [
            { key: '1', label: 'Nombre del contacto' },
            { key: '2', label: 'Mensaje personalizado' }
        ]
    }
];

async function loadScheduledMessageForPanel(contactId) {
    const container = document.getElementById('scheduledMessageContent');
    const btn = document.getElementById('scheduleMessagePanelBtn');
    if (!container || !contactId) return;

    container.innerHTML = '<p class="text-sm text-gray-400 animate-pulse">Cargando...</p>';

    try {
        const resp = await fetch(
            `${BASE_URL}/contacts/${encodeURIComponent(contactId)}/scheduled-messages`,
            { headers: { 'X-API-Key': API_KEY } }
        );
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();

        const now = new Date();
        const pending = (data.messages || []).filter(
            m => m.status === 'pending' && new Date(m.scheduled_dt) >= now
        ).sort((a, b) => new Date(a.scheduled_dt) - new Date(b.scheduled_dt));

        if (btn) btn.classList.remove('hidden');

        if (pending.length === 0) {
            container.innerHTML = '<p class="text-sm text-gray-400">Sin mensaje programado</p>';
            return;
        }

        const next = pending[0];
        const dt = new Date(next.scheduled_dt);
        const dateStr = dt.toLocaleDateString('es-CO', { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'America/Bogota' });
        const timeStr = dt.toLocaleTimeString('es-CO', { hour: '2-digit', minute: '2-digit', hour12: true, timeZone: 'America/Bogota' });

        container.innerHTML = `
            <div class="appt-card">
                <div class="flex items-start justify-between gap-2">
                    <div class="flex-1 min-w-0">
                        <p class="text-sm font-semibold text-gray-800">📅 ${dateStr}</p>
                        <p class="text-xs text-gray-600 mt-0.5">⏰ ${timeStr}</p>
                        <p class="text-xs text-gray-500 mt-0.5 truncate">📋 ${next.template_name || 'Plantilla'}</p>
                        ${next.notes ? `<p class="text-xs text-gray-400 mt-1 italic truncate">${next.notes}</p>` : ''}
                    </div>
                    <button onclick="cancelScheduledMessage('${next.id}', '${contactId}')"
                        class="text-gray-400 hover:text-red-500 transition-colors flex-shrink-0 mt-0.5" title="Cancelar mensaje">🗑</button>
                </div>
                ${pending.length > 1 ? `<p class="text-xs text-gray-400 mt-2">+${pending.length - 1} mensaje(s) más</p>` : ''}
            </div>`;

    } catch (err) {
        console.warn('[Panel] Error cargando mensaje programado:', err);
        container.innerHTML = '<p class="text-xs text-red-400">Error al cargar</p>';
        if (btn) btn.classList.remove('hidden');
    }
}

function openScheduleMessageModal() {
    if (!currentContactId) return;

    const modal = document.getElementById('scheduleMessageModal');
    if (!modal) return;

    // Reset form
    document.getElementById('scheduleMessageForm').reset();
    document.getElementById('smResult').classList.add('hidden');
    document.getElementById('smSubmitBtn').disabled = false;
    document.getElementById('smSubmitBtn').textContent = 'Programar Envío';

    // Poblar dropdown de templates
    const select = document.getElementById('smTemplateSelect');
    select.innerHTML = SCHEDULABLE_TEMPLATES.map(t =>
        `<option value="${t.name}">${t.label}</option>`
    ).join('');

    // Renderizar campos de variables para la primera plantilla + pre-llenar nombre
    onSmTemplateChange();

    modal.classList.remove('hidden');
}

function closeScheduleMessageModal() {
    document.getElementById('scheduleMessageModal')?.classList.add('hidden');
}

function onSmTemplateChange() {
    const select = document.getElementById('smTemplateSelect');
    const previewBox = document.getElementById('smPreviewBox');
    const varsContainer = document.getElementById('smVarsContainer');
    if (!select || !previewBox || !varsContainer) return;

    const tpl = SCHEDULABLE_TEMPLATES.find(t => t.name === select.value);
    if (!tpl) return;

    // Renderizar inputs dinámicos para cada variable de la plantilla
    varsContainer.innerHTML = tpl.vars.map((v, i) => {
        const existingVal = document.getElementById(`smVar_${v.key}`)?.value || '';
        const defaultVal = v.key === '1' && currentName && !existingVal
            ? currentName.split(' ')[0]
            : existingVal;
        return `
        <div>
            <label class="block text-sm font-medium mb-1">
                ${v.label} <span class="text-red-500">*</span>
            </label>
            <input type="text" id="smVar_${v.key}" required oninput="updateSmPreview()"
                class="w-full border rounded px-3 py-2 focus:outline-none focus:ring-2 focus:ring-amber-400"
                placeholder="${v.key === '1' ? 'Ej: Carlos' : 'Ej: te escribo para saber si aún estás buscando'}"
                value="${defaultVal}">
        </div>`;
    }).join('');

    updateSmPreview();
}

function updateSmPreview() {
    const select = document.getElementById('smTemplateSelect');
    const previewBox = document.getElementById('smPreviewBox');
    if (!select || !previewBox) return;

    const tpl = SCHEDULABLE_TEMPLATES.find(t => t.name === select.value);
    if (!tpl) return;

    let rendered = tpl.preview;
    tpl.vars.forEach(v => {
        const val = document.getElementById(`smVar_${v.key}`)?.value.trim() || `{{${v.key}}}`;
        rendered = rendered.replace(`{${v.key}}`, val);
    });

    previewBox.textContent = `"${rendered}"`;
    previewBox.classList.remove('hidden');
}

async function submitScheduleMessage(event) {
    event.preventDefault();
    if (!currentContactId) return;

    const select = document.getElementById('smTemplateSelect');
    const datetimeInput = document.getElementById('smDatetime');
    const notesInput = document.getElementById('smNotes');
    const submitBtn = document.getElementById('smSubmitBtn');
    const resultDiv = document.getElementById('smResult');

    const tpl = SCHEDULABLE_TEMPLATES.find(t => t.name === select.value);
    if (!tpl || !datetimeInput.value) return;

    // Recoger todas las variables dinámicas
    const templateVariables = {};
    for (const v of tpl.vars) {
        const val = document.getElementById(`smVar_${v.key}`)?.value.trim() || '';
        if (!val) return;
        templateVariables[v.key] = val;
    }

    submitBtn.disabled = true;
    submitBtn.textContent = 'Programando...';
    resultDiv.classList.add('hidden');

    try {
        const resp = await fetch(
            `${BASE_URL}/contacts/${encodeURIComponent(currentContactId)}/scheduled-messages`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-API-Key': API_KEY },
                body: JSON.stringify({
                    template_sid: tpl.sid,
                    template_name: tpl.name,
                    template_variables: templateVariables,
                    scheduled_dt: datetimeInput.value,
                    advisor_id: ADVISOR_ID || null,
                    canal: currentCanal || 'whatsapp',
                    notes: notesInput.value.trim()
                })
            }
        );

        const data = await resp.json();
        const content = resultDiv.querySelector('div');

        if (resp.ok) {
            content.className = 'p-3 rounded text-sm bg-green-100 text-green-800 border border-green-200';
            const dt = new Date(data.scheduled_dt);
            const dateStr = dt.toLocaleDateString('es-CO', { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'America/Bogota' });
            const timeStr = dt.toLocaleTimeString('es-CO', { hour: '2-digit', minute: '2-digit', hour12: true, timeZone: 'America/Bogota' });
            content.textContent = `✅ Mensaje programado para ${dateStr} a las ${timeStr}`;
            resultDiv.classList.remove('hidden');

            await loadScheduledMessageForPanel(currentContactId);

            setTimeout(() => closeScheduleMessageModal(), 2000);
        } else {
            content.className = 'p-3 rounded text-sm bg-red-100 text-red-800 border border-red-200';
            content.textContent = `❌ ${data.detail || 'Error al programar el mensaje'}`;
            resultDiv.classList.remove('hidden');
            submitBtn.disabled = false;
            submitBtn.textContent = 'Programar Envío';
        }
    } catch (err) {
        console.error('[SchedMsg] Error:', err);
        submitBtn.disabled = false;
        submitBtn.textContent = 'Programar Envío';
    }
}

async function cancelScheduledMessage(messageId, contactId) {
    if (!confirm('¿Cancelar este mensaje programado?')) return;

    try {
        const resp = await fetch(`${BASE_URL}/scheduled-messages/${messageId}`, {
            method: 'DELETE',
            headers: { 'X-API-Key': API_KEY }
        });
        if (resp.ok) {
            showToast('Mensaje cancelado', 'success');
            await loadScheduledMessageForPanel(contactId);
        } else {
            showToast('Error al cancelar el mensaje', 'error');
        }
    } catch (err) {
        showToast('Error de conexión', 'error');
    }
}

/**
 * Renderiza el array de notas en #notesList.
 * @param {Array} notes - Array de objetos nota
 * @param {string} contactId - Para re-bind de acciones
 */
function renderNotes(notes, contactId) {
    const container = document.getElementById('notesList');
    if (!container) return;

    if (!notes || notes.length === 0) {
        container.innerHTML = '<p class="text-xs text-gray-400 px-1">Sin notas. Agrega la primera nota.</p>';
        return;
    }

    container.innerHTML = notes.map(note => {
        const dt = new Date(note.created_at);
        const dateStr = dt.toLocaleDateString('es-CO', { day: 'numeric', month: 'short' });
        const timeStr = dt.toLocaleTimeString('es-CO', { hour: '2-digit', minute: '2-digit', hour12: true });
        const wasEdited = note.updated_at !== note.created_at;
        const escapedContent = note.content.replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        return `
        <div class="note-card" id="note-${note._id}">
            <div id="note-view-${note._id}">
                <p class="text-sm text-gray-800 leading-snug whitespace-pre-wrap">${note.content.replace(/</g, '&lt;').replace(/>/g, '&gt;')}</p>
                <div class="flex items-center justify-between mt-2">
                    <span class="text-xs text-gray-400">${note.advisor_name || 'Asesor'} · ${dateStr} ${timeStr}${wasEdited ? ' · editada' : ''}</span>
                    <div class="flex gap-2">
                        <button onclick="editNote('${note._id}', '${escapedContent}', '${contactId}')" class="text-xs text-gray-400 hover:text-blue-600 transition-colors" title="Editar">✏️</button>
                        <button onclick="deleteNote('${note._id}', '${contactId}')" class="text-xs text-gray-400 hover:text-red-500 transition-colors" title="Eliminar">🗑</button>
                    </div>
                </div>
            </div>
            <div id="note-edit-${note._id}" class="hidden note-card-edit">
                <textarea id="note-edit-text-${note._id}" class="w-full text-sm" rows="3">${note.content.replace(/</g, '&lt;')}</textarea>
                <div class="flex justify-end gap-2 mt-2">
                    <button onclick="cancelEditNote('${note._id}')" class="text-xs text-gray-500 hover:text-gray-700">Cancelar</button>
                    <button onclick="confirmEditNote('${note._id}', '${contactId}')" class="text-xs font-semibold text-[#1A1A1A] bg-[#F5C400] px-3 py-1 rounded-full hover:bg-[#D4A800]">Guardar</button>
                </div>
            </div>
        </div>`;
    }).join('');
}

/**
 * Muestra el inline editor para una nota específica.
 * @param {string} noteId
 * @param {string} currentContent
 */
function editNote(noteId, currentContent, _contactId) {
    document.getElementById(`note-view-${noteId}`)?.classList.add('hidden');
    const editEl = document.getElementById(`note-edit-${noteId}`);
    if (editEl) {
        editEl.classList.remove('hidden');
        const ta = document.getElementById(`note-edit-text-${noteId}`);
        if (ta) { ta.value = currentContent.replace(/&quot;/g, '"').replace(/&lt;/g, '<').replace(/&gt;/g, '>'); ta.focus(); }
    }
}

/** Cancela la edición y restaura la vista. */
function cancelEditNote(noteId) {
    document.getElementById(`note-view-${noteId}`)?.classList.remove('hidden');
    document.getElementById(`note-edit-${noteId}`)?.classList.add('hidden');
}

/**
 * Confirma y guarda la edición de una nota via PATCH.
 * @param {string} noteId
 * @param {string} contactId
 */
async function confirmEditNote(noteId, contactId) {
    const ta = document.getElementById(`note-edit-text-${noteId}`);
    const content = ta?.value?.trim();
    if (!content) return;

    try {
        const resp = await fetch(
            `${BASE_URL}/contacts/${encodeURIComponent(contactId)}/notes/${encodeURIComponent(noteId)}`,
            {
                method: 'PATCH',
                headers: { 'X-API-Key': API_KEY, 'Content-Type': 'application/json' },
                body: JSON.stringify({ content })
            }
        );
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        renderNotes(data.notes || [], contactId);
    } catch (err) {
        console.error('[Panel] Error actualizando nota:', err);
        showToast('Error al guardar la nota', 'error');
    }
}

/**
 * Elimina (soft delete) una nota via DELETE.
 * @param {string} noteId
 * @param {string} contactId
 */
async function deleteNote(noteId, contactId) {
    if (!confirm('¿Eliminar esta nota?')) return;

    try {
        const resp = await fetch(
            `${BASE_URL}/contacts/${encodeURIComponent(contactId)}/notes/${encodeURIComponent(noteId)}`,
            {
                method: 'DELETE',
                headers: { 'X-API-Key': API_KEY }
            }
        );
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        renderNotes(data.notes || [], contactId);
    } catch (err) {
        console.error('[Panel] Error eliminando nota:', err);
        showToast('Error al eliminar la nota', 'error');
    }
}

/**
 * Guarda una nueva nota via POST y limpia el formulario.
 */
async function saveNewNote() {
    const ta = document.getElementById('newNoteText');
    const content = ta?.value?.trim();
    if (!content) return;

    const contactId = currentContactId;
    if (!contactId) { showToast('Selecciona un contacto primero', 'warning'); return; }

    const advisorId = ADVISOR_ID || '';
    const advisorName = ADVISOR_NAME || '';

    try {
        const resp = await fetch(
            `${BASE_URL}/contacts/${encodeURIComponent(contactId)}/notes`,
            {
                method: 'POST',
                headers: { 'X-API-Key': API_KEY, 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    content,
                    advisor_id: advisorId,
                    advisor_name: advisorName,
                    phone: currentPhone || ''
                })
            }
        );
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        renderNotes(data.notes || [], contactId);
        // Limpiar y cerrar formulario
        if (ta) ta.value = '';
        cancelAddNote();
        showToast('Nota guardada', 'success');
    } catch (err) {
        console.error('[Panel] Error guardando nota:', err);
        showToast('Error al guardar la nota', 'error');
    }
}

async function sendMessage(e) {
    e.preventDefault();
    console.log('[Panel] sendMessage() iniciado');

    const phone = document.getElementById('selectedPhone').value;
    const contactId = document.getElementById('selectedContactId').value;
    const message = document.getElementById('messageInput').value.trim();
    const resultDiv = document.getElementById('sendResult');

    console.log('[Panel] Datos de envio:', { phone, contactId, messageLength: message.length, hasMedia: !!selectedMediaFile });

    // --- TEMPLATE ACTIVO: interceptar y enviar como template ---
    if (activeTemplateId && message && !selectedMediaFile) {
        if (/\{\w+\}/.test(message)) {
            // Quedan variables sin rellenar
            resultDiv.className = 'mt-2 text-sm text-red-600';
            resultDiv.textContent = 'Reemplaza todos los campos {variable} antes de enviar';
            resultDiv.classList.remove('hidden');
            const inp = document.getElementById('messageInput');
            if (inp) jumpToNextVariable(inp, 1, true);
            setTimeout(() => resultDiv.classList.add('hidden'), 4000);
            return;
        }
        const _tplData = templatesData.find(t => t.id === activeTemplateId);
        if (_tplData && !_tplData.content_sid) {
            // Template sin aprobación Meta (ej: Confirmación de Cita)
            // → enviar el texto editado directamente como mensaje plano.
            // El pipeline de extractVariableValues + SafeDict es frágil con emojis/newlines
            // y causa envío de placeholders crudos ({fecha}, {hora}) cuando falla.
            console.log('[Panel][Template] Sin content_sid, enviando como texto plano:', activeTemplateId);
            activeTemplateId = null; activeTemplateBody = ''; activeTemplateVars = [];
            const hint = document.getElementById('templateHint');
            if (hint) hint.classList.add('hidden');
            // NO hacer return → continuar al flujo normal de sendMessage() con el texto editado
        } else {
            // Template con content_sid → pipeline de extracción (necesario para Twilio Content API)
            const vars = extractVariableValues(activeTemplateBody, message, activeTemplateVars) || {};
            // Verificar que TODAS las variables tienen valor — si no, avisar a la asesora
            const missingVars = activeTemplateVars.filter(v => !vars[v] || !vars[v].trim());
            if (missingVars.length > 0) {
                resultDiv.className = 'mt-2 text-sm text-red-600';
                resultDiv.textContent =
                    `No se pudieron extraer las variables: ${missingVars.join(', ')}. ` +
                    `¿Modificaste el texto fijo de la plantilla? Selecciónala de nuevo con /`;
                resultDiv.classList.remove('hidden');
                setTimeout(() => resultDiv.classList.add('hidden'), 6000);
                return;
            }
            const tplId = activeTemplateId;
            activeTemplateId = null; activeTemplateBody = ''; activeTemplateVars = [];
            const hint = document.getElementById('templateHint');
            if (hint) hint.classList.add('hidden');
            await sendTemplateFromInput(tplId, vars);
            return;
        }
    }

    // Validar que haya contenido (texto o archivo)
    if (!phone || (!message && !selectedMediaFile)) {
        console.warn('[Panel] Validacion fallida: phone vacio o sin contenido');
        resultDiv.className = 'mt-2 text-sm text-red-600';
        resultDiv.textContent = 'Selecciona un contacto y escribe un mensaje o adjunta un archivo';
        resultDiv.classList.remove('hidden');
        return;
    }

    // Deshabilitar mientras envia
    document.getElementById('sendBtn').disabled = true;
    document.getElementById('messageInput').disabled = true;
    document.getElementById('attachBtn').disabled = true;

    try {
        const formData = new FormData();
        formData.append('to', phone);
        formData.append('contact_id', contactId);

        // Agregar mensaje de texto si existe
        if (message) {
            formData.append('body', message);
        }

        // Agregar archivo multimedia si existe
        if (selectedMediaFile) {
            formData.append('media_file', selectedMediaFile);
            console.log('[Panel] Adjuntando archivo:', selectedMediaFile.name, selectedMediaFile.type);
        }

        // Incluir canal para segregacion correcta
        if (currentCanal) {
            formData.append('canal', currentCanal);
        }

        // Incluir contexto de cita si existe
        if (replyToMessage) {
            formData.append('reply_to_id', replyToMessage.id);
            formData.append('reply_to_preview', JSON.stringify({
                sender: replyToMessage.sender,
                sender_name: replyToMessage.sender_name,
                content: replyToMessage.content,
                media_type: replyToMessage.media_type,
                timestamp: replyToMessage.timestamp
            }));
        }

        console.log('[Panel] Enviando POST a:', `${BASE_URL}/send-message`, 'canal:', currentCanal);

        const response = await fetch(`${BASE_URL}/send-message`, {
            method: 'POST',
            headers: { 'X-API-Key': API_KEY },
            body: formData
        });

        console.log('[Panel] Respuesta HTTP:', response.status, response.statusText);

        const data = await response.json();
        console.log('[Panel] Respuesta JSON:', data);

        if (data.status === 'success') {
            resultDiv.className = 'mt-2 text-sm text-green-600';

            // Mensaje diferente si incluia multimedia
            if (data.media_type) {
                const mediaLabel = data.media_type === 'image' ? 'imagen' : (data.media_type === 'video' ? 'video' : (data.media_type === 'audio' ? 'audio' : 'archivo'));
                resultDiv.textContent = `Mensaje con ${mediaLabel} enviado correctamente`;
            } else {
                resultDiv.textContent = 'Mensaje enviado correctamente';
            }

            document.getElementById('messageInput').value = '';

            // Limpiar seleccion de archivo y reply
            clearMediaSelection();
            const _replySnapshot = replyToMessage; // capturar antes de limpiar
            cancelReply();

            // OPTIMISTIC UI: Mostrar burbuja inmediatamente sin esperar query a MongoDB
            // Elimina el "efecto fantasma" (~500ms de chat vacío tras enviar).
            // renderChatBubbles() limpiará esta burbuja y la reemplazará con el dato real.
            const _chatContainer = document.getElementById('chatMessages');
            if (_chatContainer && currentContactId === contactId) {
                const _emptyMsg = _chatContainer.querySelector('[data-empty-msg]');
                if (_emptyMsg) _emptyMsg.remove();

                let _mediaHtml = '';
                if (data.media_type === 'image' && data.media_url) {
                    _mediaHtml = `<div class="mb-2"><img src="${data.media_url}" alt="Imagen" class="max-w-full rounded-lg cursor-pointer hover:opacity-90 transition-opacity" style="max-height:300px;object-fit:contain;" onclick="window.open('${data.media_url}','_blank')"></div>`;
                } else if (data.media_type === 'video' && data.media_url) {
                    _mediaHtml = `<div class="mb-2"><video src="${data.media_url}" controls preload="metadata" class="max-w-full rounded-lg" style="max-height:300px;max-width:100%;">Tu navegador no soporta video.</video></div>`;
                } else if (data.media_type === 'audio' && data.media_url) {
                    const _optPid = `ap-opt-${Date.now()}`;
                    _mediaHtml = buildAudioPlayerHtml(data.media_url, _detectAudioType(data.media_url), _optPid);
                } else if (data.media_url) {
                    _mediaHtml = `<div class="mb-2"><a href="${data.media_url}" target="_blank" class="text-blue-600 underline text-sm">Ver archivo</a></div>`;
                }

                // Cita en burbuja optimista
                let _quoteHtml = '';
                if (_replySnapshot) {
                    const _rp = _replySnapshot;
                    const _rpColor = _rp.sender === 'client' ? '#6B7280' : (_rp.sender === 'bot' ? '#D97706' : '#2563EB');
                    let _rpText = escapeHtml((_rp.content || '').substring(0, 80));
                    if (_rp.media_type && !_rp.content) {
                        _rpText = { image: '📷 Imagen', audio: '🎵 Audio', video: '🎬 Video', document: '📄 Documento' }[_rp.media_type] || '📎 Archivo';
                    }
                    _quoteHtml = `
                        <div class="reply-quote mb-2 p-2 rounded" style="background:rgba(0,0,0,0.04);border-left:3px solid ${_rpColor};">
                            <p class="text-xs font-semibold" style="color:${_rpColor};">${escapeHtml(_rp.sender_name || _rp.sender)}</p>
                            <p class="text-xs text-gray-500 truncate">${_rpText}</p>
                        </div>`;
                }

                const _senderName = ADVISOR_NAME || 'Asesor';
                const _tempId = `temp-${Date.now()}`;
                const _msgText = message; // capturado antes de limpiar el input

                const _bubble = `
                    <div class="flex justify-end mb-3 animate-fadeIn" data-msg-id="${_tempId}" data-optimistic="true">
                        <div class="bubble-advisor p-3 shadow-sm">
                            <p class="text-xs font-semibold text-gray-600 mb-1">${escapeHtml(_senderName)}</p>
                            ${_quoteHtml}
                            ${_mediaHtml}
                            ${_msgText ? `<p class="text-gray-800 whitespace-pre-wrap">${escapeHtml(_msgText)}</p>` : ''}
                            <p class="text-xs text-gray-500 text-right mt-1">${formatBogotaTime(new Date().toISOString())}</p>
                        </div>
                    </div>`;

                _chatContainer.insertAdjacentHTML('beforeend', _bubble);
                _chatContainer.scrollTo({ top: _chatContainer.scrollHeight, behavior: 'smooth' });
            }

            // ARQUITECTURA v2.0: MongoDB es la fuente de verdad en tiempo real
            // El mensaje ya esta disponible en MongoDB (~5ms), no necesitamos
            // polling incremental complejo. Un refresh inmediato es suficiente.
            loadChatHistory(contactId);
            scheduleContactsRefresh(); // Reordenar la lista inmediatamente
        } else if (data.status === 'warning') {
            console.warn('[Panel] Warning del servidor:', data.message);
            resultDiv.className = 'mt-2 text-sm text-orange-600';
            resultDiv.textContent = data.message;
        } else {
            throw new Error(data.detail || data.message || 'Error desconocido');
        }

    } catch (error) {
        console.error('[Panel] Error en sendMessage:', error);
        resultDiv.className = 'mt-2 text-sm text-red-600';
        resultDiv.textContent = `Error: ${error.message}`;
    } finally {
        document.getElementById('sendBtn').disabled = false;
        document.getElementById('messageInput').disabled = false;
        document.getElementById('attachBtn').disabled = false;
        resultDiv.classList.remove('hidden');

        // Ocultar mensaje despues de 5 segundos
        setTimeout(() => resultDiv.classList.add('hidden'), 5000);
    }
}

// Funcion para enviar template cuando la ventana esta cerrada
async function sendTemplateMessage() {
    console.log('[Panel] sendTemplateMessage() iniciado');

    const phone = document.getElementById('selectedPhone').value;
    const contactId = document.getElementById('selectedContactId').value;
    const resultDiv = document.getElementById('sendResult');
    const selector = document.getElementById('templateSelector');
    const templateId = selector ? selector.value : 'reactivacion_general';

    if (!phone) {
        alert('Selecciona un contacto primero');
        return;
    }

    if (!templateId) {
        alert('Selecciona un template');
        return;
    }

    // Obtener template y sus variables
    const template = templatesData.find(t => t.id === templateId);
    if (!template) {
        alert('Template no encontrado');
        return;
    }

    // Pedir valores de variables si las hay
    const variables = {};
    const varList = template.variables || [];

    // Obtener nombre del contacto del header
    const contactName = document.getElementById('contactName')?.textContent || '';

    for (const varName of varList) {
        let defaultValue = '';
        // Pre-rellenar nombre si esta disponible
        if (varName === 'nombre' && contactName && contactName !== 'Selecciona un contacto') {
            defaultValue = contactName.split(' ')[0];  // Primer nombre
        }

        const value = prompt(`Valor para {${varName}}:`, defaultValue);
        if (value === null) {
            // Usuario cancelo
            return;
        }
        variables[varName] = value;
    }

    // Mostrar preview del mensaje final
    let previewMsg = template.body;
    for (const [key, val] of Object.entries(variables)) {
        previewMsg = previewMsg.replace(new RegExp(`\\{${key}\\}`, 'g'), val || `{${key}}`);
    }

    if (!confirm(`Enviar este mensaje?\n\n${previewMsg}`)) {
        return;
    }

    // Deshabilitar boton mientras envia (si existe)
    const templateBtn = document.getElementById('sendTemplateBtn');
    if (templateBtn) { templateBtn.disabled = true; templateBtn.textContent = 'Enviando...'; }

    try {
        const formData = new FormData();
        formData.append('to', phone);
        formData.append('contact_id', contactId);
        formData.append('template_id', templateId);
        formData.append('variables', JSON.stringify(variables));
        if (ADVISOR_ID) formData.append('advisor_id', ADVISOR_ID);
        // Incluir canal para segregacion correcta
        if (currentCanal) {
            formData.append('canal', currentCanal);
        }

        console.log('[Panel] Enviando POST template a:', `${BASE_URL}/send-template`, 'canal:', currentCanal);

        const response = await fetch(`${BASE_URL}/send-template`, {
            method: 'POST',
            headers: { 'X-API-Key': API_KEY },
            body: formData
        });

        const data = await response.json();
        console.log('[Panel] Respuesta template:', data);

        if (data.status === 'success') {
            resultDiv.className = 'mt-2 text-sm text-green-600';
            resultDiv.textContent = `Template "${template.name}" enviado correctamente.`;
            resultDiv.classList.remove('hidden');

            // Ocultar warning y preview
            document.getElementById('windowWarning').classList.add('hidden');
            document.getElementById('templatePreview')?.classList.add('hidden');

            // Resetear selector
            if (selector) selector.value = '';

            // ARQUITECTURA v2.0: MongoDB es la fuente de verdad en tiempo real
            // El mensaje ya esta disponible inmediatamente
            loadChatHistory(contactId);
        } else {
            throw new Error(data.detail || data.message || 'Error enviando template');
        }

    } catch (error) {
        console.error('[Panel] Error en sendTemplateMessage:', error);
        resultDiv.className = 'mt-2 text-sm text-red-600';
        resultDiv.textContent = `Error: ${error.message}`;
        resultDiv.classList.remove('hidden');
    } finally {
        if (templateBtn) { templateBtn.disabled = false; templateBtn.textContent = 'Enviar Template'; }

        // Ocultar mensaje despues de 5 segundos
        setTimeout(() => resultDiv.classList.add('hidden'), 5000);
    }
}

// Envío de template desde el messageInput (slash command flow)
async function sendTemplateFromInput(templateId, variables) {
    const phone = document.getElementById('selectedPhone').value;
    const contactId = document.getElementById('selectedContactId').value;
    const resultDiv = document.getElementById('sendResult');
    const template = templatesData.find(t => t.id === templateId);

    if (!template || !phone) return;

    document.getElementById('sendBtn').disabled = true;
    document.getElementById('messageInput').disabled = true;
    document.getElementById('attachBtn').disabled = true;

    try {
        const formData = new FormData();
        formData.append('to', phone);
        formData.append('contact_id', contactId);
        formData.append('template_id', templateId);
        formData.append('variables', JSON.stringify(variables));
        if (ADVISOR_ID) formData.append('advisor_id', ADVISOR_ID);
        if (currentCanal) formData.append('canal', currentCanal);

        const response = await fetch(`${BASE_URL}/send-template`, {
            method: 'POST',
            headers: { 'X-API-Key': API_KEY },
            body: formData
        });
        const data = await response.json();

        if (data.status === 'success') {
            resultDiv.className = 'mt-2 text-sm text-green-600';
            resultDiv.textContent = `Plantilla "${template.name}" enviada correctamente.`;
            resultDiv.classList.remove('hidden');
            document.getElementById('messageInput').value = '';
            document.getElementById('windowWarning')?.classList.add('hidden');
            loadChatHistory(contactId);
            // Re-verificar ventana: el template de reactivación puede haberla abierto
            const _phone = document.getElementById('selectedPhone')?.value;
            if (_phone) setTimeout(() => checkWindowStatus(_phone), 1500);
        } else {
            throw new Error(data.detail || data.message || 'Error enviando plantilla');
        }
    } catch (err) {
        console.error('[Panel] Error en sendTemplateFromInput:', err);
        resultDiv.className = 'mt-2 text-sm text-red-600';
        resultDiv.textContent = err.message || 'Error enviando plantilla';
        resultDiv.classList.remove('hidden');
    } finally {
        document.getElementById('sendBtn').disabled = false;
        // Solo re-habilitar messageInput si la ventana está abierta
        if (currentWindowOpen) {
            document.getElementById('messageInput').disabled = false;
            document.getElementById('attachBtn').disabled = false;
        } else {
            // Ventana sigue cerrada: re-deshabilitar y mostrar botón "/"
            const inp = document.getElementById('messageInput');
            if (inp) {
                inp.disabled = true;
                inp.placeholder = 'Ventana cerrada. Usa un template para reactivar.';
                inp.classList.add('bg-gray-200', 'cursor-not-allowed');
            }
            _applyWindowClosedUI(true);
        }
        setTimeout(() => resultDiv.classList.add('hidden'), 5000);
    }
}

// =========================================================================
// POLLING con Page Visibility API
// =========================================================================

// Estado de visibilidad de la pestaña
let isTabVisible = true;
let pendingRefresh = false;  // Si hay refresh pendiente cuando la pestaña estaba oculta

function startPolling() {
    if (pollingInterval) clearInterval(pollingInterval);

    // Usar intervalo mas rapido si hay chat activo
    const interval = currentContactId ? POLLING_INTERVAL_ACTIVE : POLLING_INTERVAL_IDLE;

    pollingInterval = setInterval(async () => {
        // Solo hacer polling si la pestaña está visible
        if (!isTabVisible) {
            pendingRefresh = true;
            console.log('[Panel] Polling omitido - pestaña inactiva');
            return;
        }

        // Actualizar lista de contactos
        await loadContacts();

        // Actualizar historial solo si WS no está activo (evita doble carga)
        const wsActive = ws && ws.readyState === WebSocket.OPEN;
        if (!wsActive && currentContactId) {
            await loadChatHistory(currentContactId);
        }
    }, interval);

    console.log(`[Panel] Polling iniciado con intervalo: ${interval}ms`);
}

// Reiniciar polling cuando cambia el estado del chat (para ajustar intervalo)
function restartPollingForChat() {
    startPolling();
}

function stopPolling() {
    if (pollingInterval) {
        clearInterval(pollingInterval);
        pollingInterval = null;
    }
}

/**
 * Polling de respaldo cuando el WebSocket está activo.
 * Solo refresca la lista de contactos (sin historial de chat) cada 10s.
 * El historial lo maneja el WS via contact_updated.
 */
function startFallbackPolling() {
    if (pollingInterval) clearInterval(pollingInterval);
    // [StageFilter] Cuando hay filtro de etapa activo, cada loadContacts() golpea HubSpot.
    // Reducir cadencia a 30s para no saturar HubSpot ni el cache.
    const _intervalMs = activeStageFilter ? 30000 : POLLING_INTERVAL_IDLE;
    pollingInterval = setInterval(async () => {
        if (!isTabVisible) return;
        await loadContacts();
    }, _intervalMs);
    console.log(`[Panel] Fallback polling activo (WS conectado): ${_intervalMs / 1000}s${activeStageFilter ? ' [stage filter]' : ''}`);
}

/**
 * Maneja cambios de visibilidad de la pestaña.
 * Optimización: Pausa polling cuando el usuario no está mirando.
 */
function handleVisibilityChange() {
    if (document.hidden) {
        // Pestaña oculta: marcar como invisible
        isTabVisible = false;
        console.log('[Panel] Pestaña oculta - polling pausado');
    } else {
        // Pestaña visible: reactivar
        isTabVisible = true;
        console.log('[Panel] Pestaña visible - polling activo');

        // Si hay refresh pendiente, ejecutarlo inmediatamente
        if (pendingRefresh) {
            pendingRefresh = false;
            console.log('[Panel] Ejecutando refresh pendiente...');
            loadContacts();
            if (currentContactId) {
                loadChatHistory(currentContactId);
            }
        }

        // Revisar contactos BOT_ACTIVE sin nombre al volver a la pestaña
        checkSilentBotContacts();
    }
}

// =========================================================================
// EVENT LISTENERS
// =========================================================================

document.addEventListener('DOMContentLoaded', () => {
    console.log('[Panel] Inicializando panel de asesores...');

    // Actualizar header si hay advisor filtrado
    if (ADVISOR_NAME) {
        document.getElementById('panelTitle').textContent = `Panel de ${ADVISOR_NAME}`;
        document.getElementById('panelSubtitle').textContent = 'Mis contactos asignados';
    }

    // Cargar contactos iniciales
    loadContacts();

    // Cargar templates disponibles
    loadTemplates();

    // Cargar workers para el filtro de citas
    loadWorkerFilterOptions();

    // Poblar el select de filtro por etapa con PIPELINE_STAGES
    _initStageFilter();

    // Inicializar listeners del template picker (slash command)
    _initTemplatePickerListeners();

    // Iniciar polling
    startPolling();

    // Revisión periódica de contactos BOT_ACTIVE sin nombre (cada 5 min)
    setInterval(checkSilentBotContacts, 5 * 60 * 1000);

    // Polling de notificaciones backend (cada 30s)
    if (ADVISOR_ID) {
        pollNotifications();
        setInterval(pollNotifications, NOTIF_POLL_INTERVAL);
    }

    // Sin fecha por defecto: el panel arranca mostrando todos los contactos activos.
    // El usuario elige un día específico para filtrar historial.
    (function _initDefaultDates() {})();

    // Esc: deseleccionar contacto activo y limpiar URL (solo si no hay modal/picker abierto)
    document.addEventListener('keydown', (e) => {
        if (e.key !== 'Escape') return;
        // Si el template picker está abierto, dejarlo gestionar el Esc
        const pickerOpen = document.getElementById('templatePicker')?.classList.contains('hidden') === false
            || document.getElementById('templatePickerPopup')?.style.display === 'block';
        if (pickerOpen) return;
        // Si hay algún modal visible, no deseleccionar
        const modalOpen = document.querySelector('.fixed.inset-0:not(.hidden)');
        if (modalOpen) return;
        if (currentContactId) deselectContact();
    });

    // Right-click en contactos → menú "Marcar como no leído"
    const _contactsList = document.getElementById('contactsList');
    if (_contactsList) {
        _contactsList.addEventListener('contextmenu', (e) => {
            const contactEl = e.target.closest('.contact-item[data-phone]');
            if (!contactEl) return;
            e.preventDefault();
            const phone = contactEl.getAttribute('data-phone');
            if (phone) _showUnreadContextMenu(e.clientX, e.clientY, phone);
        });
    }

    // Boton refresh
    document.getElementById('refreshBtn').addEventListener('click', loadContacts);

    // Cambio en radio de campo de fecha → recargar
    document.querySelectorAll('input[name="dateField"]').forEach(r => {
        r.addEventListener('change', loadContacts);
    });

    // Cambio en input de fecha → recargar automáticamente
    const _dateFrom = document.getElementById('dateFrom');
    if (_dateFrom) _dateFrom.addEventListener('change', loadContacts);

    // Enviar mensaje - Form submit
    const sendForm = document.getElementById('sendForm');
    if (sendForm) {
        sendForm.addEventListener('submit', sendMessage);
        console.log('[Panel] Event listener de sendForm configurado');
    } else {
        console.error('[Panel] ERROR: No se encontro el formulario sendForm');
    }

    // Enviar con Enter (Shift+Enter inserta nueva línea)
    const messageInput = document.getElementById('messageInput');
    if (messageInput) {
        messageInput.addEventListener('keydown', function (e) {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                console.log('[Panel] Enter presionado - enviando mensaje');
                sendMessage(e);
            }
        });
        console.log('[Panel] Event listener de Enter configurado');
    }

    console.log('[Panel] Inicializacion completada');
});

// Detener polling cuando se cierra la pestana
window.addEventListener('beforeunload', stopPolling);

// Page Visibility API: Pausar/reanudar polling segun visibilidad
document.addEventListener('visibilitychange', handleVisibilityChange);

// =========================================================================
// WEBSOCKET PARA NOTIFICACIONES EN TIEMPO REAL
// =========================================================================

let ws = null;
let wsReconnectAttempts = 0;
const WS_MAX_RECONNECT_ATTEMPTS = 5;
const WS_RECONNECT_DELAY = 3000;  // 3 segundos (legacy — ahora se usa backoff exponencial)
const WS_BASE_RECONNECT_DELAY = 1000;  // 1s base para backoff exponencial
let wsCurrentDelay = WS_BASE_RECONNECT_DELAY;

/**
 * Conecta al WebSocket para recibir notificaciones en tiempo real.
 * Usa el ADVISOR_ID de la URL si esta disponible.
 */
function connectWebSocket() {
    // Guard: abortar si ya hay WS activo o conectando — previene conexiones duplicadas
    // (servidor mostraba Total: 1→2→3 en cada F5, causando múltiples loadContacts() y 429s)
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
        console.log('[Panel] WS ya activo (readyState:', ws.readyState, ') — reconexión ignorada');
        return;
    }
    // Si está cerrando: limpiar handlers para evitar bucle recursivo de reconexión
    if (ws && ws.readyState === WebSocket.CLOSING) {
        ws.onclose = null;
        ws = null;
    }

    // Si no hay ADVISOR_ID, conectar con "all" para recibir broadcasts
    const advisorId = ADVISOR_ID || 'all';

    // Determinar protocolo (ws o wss segun http/https)
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/whatsapp/panel/ws/${advisorId}`;

    console.log('[Panel] Conectando WebSocket:', wsUrl);

    try {
        ws = new WebSocket(wsUrl);

        ws.onopen = () => {
            console.log('[Panel] WebSocket conectado');
            wsReconnectAttempts = 0;
            wsCurrentDelay = WS_BASE_RECONNECT_DELAY;

            // Cambiar a fallback polling (10s, sin historial) — el WS maneja eventos en tiempo real
            startFallbackPolling();

            // Fix CR-2: resync inmediato al reconectar para recuperar mensajes llegados durante la desconexión.
            // _lastContactTimestamps detectará actividad nueva → incrementará unreadCounts correctamente.
            loadContacts();

            // Notificar que contacto actual esta siendo observado
            if (currentPhone) {
                ws.send(JSON.stringify({
                    type: 'watching',
                    phone: currentPhone
                }));
            }
        };

        ws.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                handleWebSocketMessage(data);
            } catch (e) {
                console.warn('[Panel] Error parseando mensaje WS:', e);
            }
        };

        ws.onclose = (event) => {
            console.log('[Panel] WebSocket desconectado:', event.code, event.reason);
            ws = null;

            // Retomar polling activo como fallback (WS no disponible)
            startPolling();

            // Fix CR-2: reconexión infinita con backoff exponencial, sin hard limit.
            // El exponente se cap en 5 para que el delay máximo sea ~30s (1000 * 2^5 = 32s → clamp 30s).
            // Sin límite de intentos: si Railway hace un deploy, el panel reconecta solo sin recargar.
            wsReconnectAttempts++;
            wsCurrentDelay = Math.min(
                WS_BASE_RECONNECT_DELAY * Math.pow(2, Math.min(wsReconnectAttempts - 1, 5)),
                30000
            );
            console.log(`[Panel] Reintentando conexion WS (intento ${wsReconnectAttempts}) en ${wsCurrentDelay}ms...`);
            setTimeout(connectWebSocket, wsCurrentDelay);
        };

        ws.onerror = (error) => {
            console.error('[Panel] Error WebSocket:', error);
        };

    } catch (e) {
        console.error('[Panel] Error creando WebSocket:', e);
    }
}

/**
 * Actualiza el badge de no-leídos de un contacto directamente en el DOM,
 * sin necesidad de re-renderizar la lista completa.
 * @param {string} phone - Teléfono del contacto
 * @param {number} count - Cantidad de mensajes no leídos
 */
function updateUnreadBadge(phone, count) {
    const contactEl = document.querySelector(`.contact-item[data-phone="${CSS.escape(phone)}"]`);
    if (!contactEl) {
        console.warn('[Panel][Badge] Elemento contacto no encontrado en DOM para phone:', phone);
        return;
    }
    const avatarWrap = contactEl.querySelector('.relative.flex-shrink-0');
    if (!avatarWrap) return;
    let badge = contactEl.querySelector('.unread-badge');
    if (count > 0) {
        if (!badge) {
            badge = document.createElement('span');
            badge.className = 'unread-badge absolute -top-1 -right-1 bg-red-500 text-white text-xs rounded-full min-w-[18px] h-[18px] flex items-center justify-center font-bold px-0.5 leading-none';
            avatarWrap.appendChild(badge);
        }
        badge.textContent = count > 9 ? '9+' : String(count);
        console.log('[Panel][Badge] Badge actualizado →', phone, count, 'no leídos');
    } else if (badge) {
        badge.remove();
    }
}

/**
 * Programa un refresco de la lista de contactos con trailing debounce de 150ms.
 * Agrupa múltiples eventos WS rápidos en una sola llamada a loadContacts().
 * Fix: era leading-cooldown (50ms, dropeaba eventos posteriores).
 *      Ahora es trailing debounce: resetea el timer en cada llamada, dispara
 *      loadContacts() solo 150ms después del ÚLTIMO evento. Si llegan 10 mensajes
 *      en 100ms, solo se hace 1 GET /contacts en lugar de 10.
 */
let _contactsRefreshTimer = null;
function scheduleContactsRefresh() {
    if (_contactsRefreshTimer) clearTimeout(_contactsRefreshTimer);  // Reset en cada llamada
    _contactsRefreshTimer = setTimeout(async () => {
        _contactsRefreshTimer = null;
        await loadContacts();
    }, 150);
}

/**
 * Maneja mensajes recibidos del WebSocket.
 * @param {Object} data - Mensaje parseado
 */
/**
 * Fix CR-2 (safety net para contactos nuevos): verifica que el contacto esté en la lista
 * después de recibir un evento WS. Cubre el gap residual de ensure_meta_with_channel()
 * que no puede ser movido antes del WS notify (depende de la respuesta de Sofia).
 * @param {string} phone - Teléfono a buscar
 * @param {number} retriesLeft - Máximo 3 intentos. No aumentar: si el contacto no llega
 *   en 3×700ms=2.1s es un problema del servidor, no de timing.
 */
function ensureContactVisible(phone, retriesLeft = 3) {
    if (retriesLeft <= 0) {
        console.warn('[Panel][Scroll] ensureContactVisible agotó retries para:', phone);
        return;
    }
    const found = allContacts.some(c => (c.phone || '') === phone || (c.whatsapp || '') === phone);
    if (!found) {
        console.log('[Panel][Scroll] Contacto no encontrado en lista, retry en 700ms. Intentos restantes:', retriesLeft - 1);
        setTimeout(async () => {
            await loadContacts();
            ensureContactVisible(phone, retriesLeft - 1);
        }, 700);
    }
}

function handleWebSocketMessage(data) {
    console.log('[Panel] Mensaje WS recibido:', data.type, data);

    switch (data.type) {
        case 'contact_updated':
            // Nuevo mensaje o actividad → reordenar lista INMEDIATAMENTE
            console.log('[Panel] contact_updated recibido, action:', data.action, 'phone:', data.phone);

            // Fix-B: Guard defensivo contra eventos WS para contactos que NO pertenecen
            // a este asesor. Con Fix-A activo el backend enruta targeted, pero esto protege
            // contra: (1) broadcast de fallback si meta no tiene assigned_owner_id,
            // (2) backlog durante reconexión WS, (3) cache staleness, (4) regresiones futuras.
            // Si phone no está en allContacts → silenciar beep/badge y schedule refresh.
            if (data.action === 'new_message' && data.phone && data.phone !== currentPhone) {
                const _phoneInList = allContacts.some(c => (c.phone || '') === data.phone);
                if (!_phoneInList) {
                    console.warn('[Panel][Guard] Evento WS foráneo (phone no en lista local):', data.phone);
                    if (!window._foreignProbation) window._foreignProbation = new Set();
                    if (window._foreignProbation.has(data.phone)) {
                        console.log('[Panel][Guard] Descartado — ya en probación:', data.phone);
                        break;
                    }
                    window._foreignProbation.add(data.phone);
                    setTimeout(() => window._foreignProbation.delete(data.phone), 10000);
                    // Refresh silencioso: si el contacto es legítimo aparecerá en la próxima carga.
                    // El siguiente evento WS para el mismo phone ya pasará el guard.
                    scheduleContactsRefresh();
                    break;
                }
            }

            // Si action es 'new_message', actualizar badge directamente sin re-render
            if (data.action === 'new_message' && data.phone && data.phone !== currentPhone) {
                // Guard: ignorar badge si el contacto fue cerrado recientemente
                if (recentlyClosedPhones.has(data.phone)) {
                    console.log('[Panel][Badge] Ignorando new_message WS para contacto cerrado:', data.phone);
                    scheduleContactsRefresh();
                    break;
                }
                // Fix dedup-WS: el backend puede disparar 2 notificaciones por el mismo mensaje
                // (p.ej. bot silenciado + notificación temprana). Si el mismo phone ya incrementó
                // badge por WS en los últimos 2s, solo actualizar el DOM sin volver a contar.
                const _now = Date.now();
                const _alreadyCounted = (_lastWsNotifiedTimestamps[data.phone] || 0) > _now - 2000;
                _lastWsNotifiedTimestamps[data.phone] = _now;  // Fix CR-4: marcar evento WS
                if (!_alreadyCounted) {
                    unreadCounts[data.phone] = (unreadCounts[data.phone] || 0) + 1;
                    console.log('[Panel] Incrementando unreadCounts para', data.phone, '→', unreadCounts[data.phone]);
                } else {
                    console.log('[Panel][Badge] Dedup WS (<2s) — no incrementar para', data.phone);
                }
                updateUnreadBadge(data.phone, unreadCounts[data.phone]);  // DOM directo, sin re-render
                // Sonido siempre que llegue mensaje de otro contacto (sin importar si la pestaña está visible)
                if (!_alreadyCounted) playNotificationBeep();
                if (document.hidden && !_alreadyCounted) {
                    showBrowserNotification(data.phone, 'Nuevo mensaje');
                }
            }

            // name_updated: actualizar memoria directamente sin recargar desde HubSpot.
            // HubSpot tarda ~1-3s en propagar el PATCH; scheduleContactsRefresh() aquí
            // devolvería el nombre viejo y revertería el cambio.
            if (data.action === 'name_updated' && data.phone && data.display_name) {
                const ni = allContacts.findIndex(c => c.phone === data.phone);
                if (ni !== -1) {
                    allContacts[ni].display_name = data.display_name;
                    _applyFiltersAndRender();
                }
                break;
            }

            // Fix: para new_message usar scheduleContactsRefresh (trailing debounce 150ms) en
            // lugar de loadContacts() directo. Si llegan 5 mensajes en 100ms, antes se hacían
            // 5 GET /contacts concurrentes → 5× enriquecimiento HubSpot → más 429s.
            // El scroll y ensureContactVisible se ejecutan 200ms después (>150ms debounce).
            if (data.action === 'new_message' && data.phone) {
                const _scrollPhone = data.phone;
                // [Bug2] Reorder inmediato en memoria sin esperar GET /contacts.
                // Mueve el contacto al tope de allContacts y fuerza re-render local.
                // scheduleContactsRefresh() sigue activo para sincronizar datos completos.
                const _nmIdx = allContacts.findIndex(c => c.phone === _scrollPhone);
                if (_nmIdx > 0) {
                    const _nmContact = allContacts.splice(_nmIdx, 1)[0];
                    allContacts.unshift(_nmContact);
                    _contactFingerprints.delete(_scrollPhone); // forzar reconstrucción del elemento
                    _applyFiltersAndRender();
                } else if (_nmIdx === -1) {
                    // Contacto no está en allContacts aún (ej: recién creado o fuera del filtro de fechas).
                    // scheduleContactsRefresh() lo traerá en la posición correcta cuando loadContacts() complete.
                    console.log('[Panel][Reorder] Contacto no encontrado en lista local:', _scrollPhone, '— loadContacts() lo agregará');
                }
                // _nmIdx === 0: contacto ya está en el tope, no necesita reordenarse
                scheduleContactsRefresh();
                setTimeout(() => {
                    ensureContactVisible(_scrollPhone);
                    const contactsList = document.getElementById('contactsList');
                    if (contactsList) {
                        console.log('[Panel][Scroll] Scroll al top por new_message de', _scrollPhone);
                        contactsList.scrollTo({ top: 0, behavior: 'smooth' });
                    }
                }, 200);
            } else {
                scheduleContactsRefresh();
            }
            // Si el chat activo es el que recibió el mensaje, refrescar historial y estado de ventana
            if (currentPhone && data.phone && data.phone === currentPhone) {
                // P3-D canal fix: si el backend hizo merge de canal (ej: ciencuadras → whatsapp),
                // el evento WS lleva el canal real. Actualizamos currentCanal ANTES de recargar
                // para que loadChatHistory use el canal correcto y muestre el mensaje nuevo.
                if (data.canal && data.canal !== currentCanal) {
                    console.log(`[Panel][P3-D] Canal corregido antes de recargar historial: ${currentCanal} → ${data.canal}`);
                    currentCanal = data.canal;
                }
                loadChatHistory(currentContactId);
                checkWindowStatus(currentPhone);
            }
            break;

        case 'new_message':
            handleNewMessageNotification(data);
            break;

        case 'contact_transferred':
            handleContactTransferred(data);
            break;

        case 'status_change':
            handleStatusChange(data);
            break;

        case 'transfer_request':
            showTransferRequestModal(data);
            break;

        case 'transfer_accepted':
            showToast(`Transferencia aceptada — ${data.contact_name || data.phone} es tuyo`, 'success');
            scheduleContactsRefresh();
            closeCreateContactModal();
            break;

        case 'transfer_rejected':
            showToast('El asesor rechazó la transferencia', 'warning');
            break;

        case 'template_delivery_failed':
            handleTemplateDeliveryFailed(data);
            break;

        case 'message_edited': {
            const _editEl = document.querySelector(`[data-msg-id="${data.message_id}"]`);
            if (_editEl && currentPhone && data.phone === currentPhone) {
                const _editBody = _editEl.querySelector('p[data-msg-body], p.text-gray-800');
                if (_editBody) { _editBody.textContent = data.new_content || ''; _editBody.style.display = ''; }
                const _editTime = _editEl.querySelector('p.text-xs.text-gray-500.text-right');
                if (_editTime) {
                    _editTime.querySelector('.edit-tag')?.remove();
                    const _now = new Date();
                    const _hhmm = _now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
                    _editTime.insertAdjacentHTML('beforeend', `<span class="edit-tag text-xs text-gray-400 ml-1">editado ${_hhmm}</span>`);
                }
            } else if (currentPhone && data.phone === currentPhone) {
                loadChatHistory(currentContactId);
            }
            break;
        }

        case 'message_updated': {
            // Reply context resuelto de forma asíncrona por fetch diferido Twilio API
            if (!currentPhone || data.phone !== currentPhone) break;
            const _updEl = document.querySelector(`[data-msg-id="${data.message_id}"]`);
            if (!_updEl) {
                // Mensaje no visible aún → recargar historial completo
                loadChatHistory(currentContactId);
                break;
            }
            if (data.reply_to_preview) {
                const rp = data.reply_to_preview;
                const rpColor = rp.sender === 'advisor' ? '#3b82f6' : '#6b7280';
                const rpLabel = rp.sender === 'advisor' ? 'Asesor' : 'Tú';
                const rpText  = rp.media_type ? `[${rp.media_type}]` : (rp.content || '');
                const quoteHtml = `<div class="reply-quote mb-2 p-2 rounded" style="background:rgba(0,0,0,0.04);border-left:3px solid ${rpColor};" data-injected-quote>
                    <p class="text-xs font-semibold" style="color:${rpColor}">${rpLabel}</p>
                    <p class="text-xs text-gray-600 truncate">${rpText.substring(0, 80)}</p>
                </div>`;
                // Insertar o reemplazar quote existente
                const existingQuote = _updEl.querySelector('[data-injected-quote]');
                if (existingQuote) {
                    existingQuote.outerHTML = quoteHtml;
                } else {
                    const bubble = _updEl.querySelector('.bubble-client, .bubble-advisor, [class*="bubble"]');
                    if (bubble) bubble.insertAdjacentHTML('afterbegin', quoteHtml);
                }
            }
            break;
        }

        case 'message_deleted': {
            const _delEl = document.querySelector(`[data-msg-id="${data.message_id}"]`);
            if (_delEl && currentPhone && data.phone === currentPhone) {
                const _isRight = _delEl.classList.contains('justify-end');
                _delEl.outerHTML = `<div class="flex ${_isRight ? 'justify-end' : 'justify-start'} mb-3" data-msg-id="${data.message_id}"><div class="bubble-deleted p-3 shadow-sm" style="background:#f3f4f6;border-radius:12px;max-width:70%;"><p class="text-xs text-gray-400 italic">Mensaje eliminado</p></div></div>`;
            } else if (currentPhone && data.phone === currentPhone) {
                loadChatHistory(currentContactId);
            }
            break;
        }

        case 'pong':
            // Respuesta a ping, ignorar
            break;

        case 'ping':
            // Responder con pong
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ type: 'pong' }));
            }
            break;

        default:
            console.log('[Panel] Mensaje WS desconocido:', data);
    }
}

function handleTemplateDeliveryFailed(data) {
    const msg = data.user_message || `⚠️ Plantilla no entregada al número ${data.phone}.`;
    console.warn('[Panel] Template delivery failed:', data);

    const div = document.createElement('div');
    div.className = 'fixed top-4 right-4 z-50 px-4 py-3 rounded-lg text-sm font-medium toast-error';
    div.style.maxWidth = '360px';
    div.style.lineHeight = '1.4';
    div.textContent = msg;
    document.body.appendChild(div);
    setTimeout(() => {
        div.style.transition = 'opacity 0.4s';
        div.style.opacity = '0';
        setTimeout(() => div.remove(), 400);
    }, 7000);

    if (data.phone && data.phone === currentPhone) {
        const chatContainer = document.getElementById('chat-messages');
        if (chatContainer) {
            const errBubble = document.createElement('div');
            errBubble.className = 'flex justify-end mb-2';
            errBubble.innerHTML = `
                <div class="bg-red-100 border border-red-400 text-red-700 rounded-lg px-3 py-2 text-xs max-w-xs">
                    ❌ Mensaje no entregado — este número no tiene WhatsApp activo.
                </div>`;
            chatContainer.appendChild(errBubble);
            chatContainer.scrollTop = chatContainer.scrollHeight;
        }
    }
}

/**
 * Maneja notificacion de nuevo mensaje.
 */
function handleNewMessageNotification(data) {
    console.log('[Panel] Nuevo mensaje de', data.phone, ':', data.preview);

    // Fix 1: sonido siempre, independiente de si la pestaña está visible o no
    console.log('[Panel][Sound] Disparando beep para', data.phone);
    playNotificationBeep();

    // Mostrar notificacion del navegador si la pestana esta oculta
    if (document.hidden) {
        showBrowserNotification(
            data.contact_name || data.phone,
            data.preview || 'Nuevo mensaje'
        );
    }

    // Tracking de mensajes no leídos (solo si el chat de ese contacto NO está abierto)
    if (data.phone && data.phone !== currentPhone) {
        // Guard: ignorar badge si el contacto fue cerrado recientemente
        if (recentlyClosedPhones.has(data.phone)) {
            console.log('[Panel][Badge] Ignorando new_message handler para contacto cerrado:', data.phone);
            scheduleContactsRefresh();
            return;
        }
        const _now2 = Date.now();
        const _alreadyCounted2 = (_lastWsNotifiedTimestamps[data.phone] || 0) > _now2 - 2000;
        _lastWsNotifiedTimestamps[data.phone] = _now2;  // Fix CR-4: marcar evento WS
        if (!_alreadyCounted2) {
            unreadCounts[data.phone] = (unreadCounts[data.phone] || 0) + 1;
        } else {
            console.log('[Panel][Badge] Dedup WS (<2s) en new_message para', data.phone);
        }
        updateUnreadBadge(data.phone, unreadCounts[data.phone]); // badge inmediato, sin esperar HTTP
    }

    // Fix CR-2 + Fix 2: carga inmediata + scroll al top para que el contacto en índice 0 sea visible.
    if (data.phone) {
        loadContacts().then(() => {
            ensureContactVisible(data.phone);
            const contactsList = document.getElementById('contactsList');
            if (contactsList) {
                console.log('[Panel][Scroll] Scroll al top por nuevo mensaje de', data.phone);
                contactsList.scrollTo({ top: 0, behavior: 'smooth' });
            }
        });
    } else {
        console.warn('[Panel] new_message sin phone — usando scheduleContactsRefresh');
        scheduleContactsRefresh();
    }

    // Si el contacto actual es el que envio mensaje, refrescar chat y estado de ventana
    if (currentPhone === data.phone) {
        loadChatHistory(currentContactId);
        checkWindowStatus(currentPhone);
    }
}

/**
 * Maneja notificacion de transferencia de contacto.
 */
function handleContactTransferred(data) {
    console.log('[Panel] Contacto transferido:', data);

    // Notificacion visual + sonido para transferencias entrantes
    if (data.direction === 'incoming') {
        console.log('[Panel][Sound] Disparando beep por transferencia entrante de', data.phone);
        playNotificationBeep();
        showBrowserNotification(
            'Nuevo contacto',
            `${data.contact_name || data.phone} ha sido transferido a tu panel`
        );
    }

    // Refrescar lista
    loadContacts();
    // Actualizar badge según dirección de la transferencia
    if (data.phone) {
        if (data.direction === 'incoming' && !_seenPhones.has(data.phone)) {
            // Transferencia entrante: el asesor aún no ha visto este contacto → badge
            unreadCounts[data.phone] = 1;
            updateUnreadBadge(data.phone, 1);
            console.log('[Panel][Inbox] Badge transferencia entrante para', data.phone);
        } else if (data.direction !== 'incoming') {
            // Transferencia saliente: limpiar badge propio
            unreadCounts[data.phone] = 0;
            updateUnreadBadge(data.phone, 0);
        }
    }
}

/**
 * Maneja cambio de estado de conversacion.
 */
function handleStatusChange(data) {
    console.log('[Panel] Cambio de estado:', data.phone, data.old_status, '->', data.new_status);

    // Actualizar banner si el status_change es del contacto actualmente seleccionado
    if (data.phone && data.phone === currentPhone && data.new_status) {
        const name = allContacts.find(c => c.phone === data.phone)?.display_name || data.phone;
        _updateHandoffBanner(data.new_status, name);
    }

    // Refrescar lista para actualizar badges
    loadContacts();
    // Resetear badge de no leídos si el estado es "cerrado" o similar
    if (data.phone && ['cerrado', 'cerrado ganado', 'cerrado perdido'].includes((data.new_status || '').toLowerCase())) {
        unreadCounts[data.phone] = 0;
        updateUnreadBadge(data.phone, 0);
    }
}


/**
 * Fix CR-5 + Fix 3: tono corto de notificación usando Web Audio API.
 * AudioContext compartido (_sharedAudioCtx) para resistir autoplay policy de Chrome/Edge.
 * Se desbloquea automáticamente en el primer click del usuario.
 */
let _sharedAudioCtx = null;

// Desbloquear AudioContext en la primera interacción del usuario
document.addEventListener('click', () => {
    if (!_sharedAudioCtx) {
        try { _sharedAudioCtx = new AudioContext(); } catch(e) {}
    } else if (_sharedAudioCtx.state === 'suspended') {
        _sharedAudioCtx.resume().catch(() => {});
    }
}, { once: true });

// ========== "MARCAR COMO NO LEÍDO" — Menú contextual ==========

/** Cierra cualquier menú contextual de "no leído" abierto */
function _closeUnreadContextMenu() {
    const existing = document.getElementById('_unreadContextMenu');
    if (existing) existing.remove();
}

/**
 * Muestra un mini-menú contextual para marcar/desmarcar como no leído.
 * Se posiciona donde el usuario hizo right-click.
 */
function _showUnreadContextMenu(x, y, phone) {
    _closeUnreadContextMenu();
    const isUnread = _manuallyUnread.has(phone);
    const menu = document.createElement('div');
    menu.id = '_unreadContextMenu';
    menu.className = 'fixed bg-white shadow-lg rounded-lg border border-gray-200 py-1 z-[70] min-w-[200px]';
    menu.style.left = `${Math.min(x, window.innerWidth - 220)}px`;
    menu.style.top = `${Math.min(y, window.innerHeight - 60)}px`;

    const option = document.createElement('div');
    option.className = 'px-4 py-2.5 text-sm hover:bg-gray-100 cursor-pointer flex items-center gap-2 select-none';
    if (isUnread) {
        option.innerHTML = '<span class="text-green-500">&#10003;</span> Marcar como leído';
    } else {
        option.innerHTML = '<span class="w-2.5 h-2.5 rounded-full bg-blue-500 inline-block"></span> Marcar como no leído';
    }
    option.addEventListener('click', () => {
        if (isUnread) {
            _manuallyUnread.delete(phone);
            // Remover badge visual
            const badge = document.querySelector(`.contact-item[data-phone="${CSS.escape(phone)}"] .manual-unread-badge`);
            if (badge) badge.remove();
        } else {
            _manuallyUnread.add(phone);
            // Agregar badge visual si no hay badge rojo de mensajes reales
            const contactEl = document.querySelector(`.contact-item[data-phone="${CSS.escape(phone)}"]`);
            if (contactEl && !contactEl.querySelector('.unread-badge')) {
                const avatarWrap = contactEl.querySelector('.relative.flex-shrink-0');
                if (avatarWrap && !avatarWrap.querySelector('.manual-unread-badge')) {
                    const dot = document.createElement('span');
                    dot.className = 'manual-unread-badge absolute -top-1 -right-1 bg-blue-500 rounded-full w-[12px] h-[12px]';
                    dot.title = 'Marcado como no leído';
                    avatarWrap.appendChild(dot);
                }
            }
        }
        _saveManuallyUnread();
        _closeUnreadContextMenu();
    });

    menu.appendChild(option);
    document.body.appendChild(menu);

    // Cerrar al hacer click fuera
    setTimeout(() => {
        const _handler = (e) => {
            if (!menu.contains(e.target)) {
                _closeUnreadContextMenu();
                document.removeEventListener('click', _handler, true);
                document.removeEventListener('contextmenu', _handler, true);
            }
        };
        document.addEventListener('click', _handler, true);
        document.addEventListener('contextmenu', _handler, true);
    }, 0);
}

// ========== SISTEMA DE NOTIFICACIONES BACKEND-DRIVEN ==========

// ── pollNotifications: obtiene notificaciones del backend cada 30s ──────────

async function pollNotifications() {
    if (!ADVISOR_ID) return;
    try {
        const res = await fetch(`${BASE_URL}/notifications?advisor=${encodeURIComponent(ADVISOR_ID)}`, {
            headers: { 'X-API-Key': API_KEY }
        });
        if (!res.ok) return;
        const data = await res.json();
        const notifications = data.notifications || [];

        // Detectar notificaciones nuevas (no vistas aún) para mostrar toast
        const currentIds = new Set(notifications.map(n => n.id));
        const newNotifs = notifications.filter(n => !_seenNotifIds.has(n.id));
        _seenNotifIds = currentIds;

        // Mostrar toast para cada notificación nueva (máx 3 simultáneos)
        newNotifs.slice(0, 3).forEach(n => showNotifToast(n));

        // Actualizar badge en campana
        const badge = document.getElementById('notifBadge');
        if (badge) {
            const count = notifications.length;
            if (count > 0) {
                badge.textContent = count > 99 ? '99+' : String(count);
                badge.classList.remove('hidden');
            } else {
                badge.classList.add('hidden');
            }
        }

        // Renderizar lista en dropdown
        renderNotifList(notifications);
    } catch (e) {
        console.warn('[Notif] Error polling notificaciones:', e);
    }
}

function showNotifToast(notif) {
    const container = document.getElementById('notifToastContainer');
    if (!container) return;

    const isInactivity = notif.type === 'inactivity_24h';
    const isAprobados  = notif.type === 'aprobados_daily';

    let icon = '🔔';
    let title = '';
    let subtitle = '';
    let borderColor = 'border-l-yellow-400';

    if (isInactivity) {
        icon = '⏰';
        title = notif.contact_name || notif.phone || 'Contacto';
        subtitle = `Lleva ${notif.hours_waiting || '+24'}h esperando respuesta`;
        borderColor = 'border-l-red-400';
    } else if (isAprobados) {
        icon = '📢';
        title = 'Contactos aprobados';
        subtitle = notif.message || '';
        borderColor = 'border-l-blue-400';
    }

    const toast = document.createElement('div');
    toast.className = `pointer-events-auto flex items-start gap-3 bg-white border border-gray-200 border-l-4 ${borderColor} rounded-lg shadow-lg px-4 py-3 cursor-pointer select-none`;
    toast.style.cssText = 'opacity:0; transform:translateX(20px); transition:opacity 0.3s,transform 0.3s;';
    toast.dataset.notifId = notif.id;

    toast.innerHTML = `
        <span class="text-lg leading-none mt-0.5">${icon}</span>
        <div class="flex-1 min-w-0">
            <p class="font-semibold text-sm text-gray-900 truncate">${title}</p>
            <p class="text-xs text-gray-500 mt-0.5 leading-relaxed">${subtitle}</p>
        </div>
        <button class="flex-shrink-0 text-gray-300 hover:text-gray-500 p-1" onclick="event.stopPropagation();this.closest('[data-notif-id]').remove()">✕</button>
    `;

    if (isInactivity && notif.phone) {
        toast.addEventListener('click', (e) => {
            if (e.target.tagName === 'BUTTON') return;
            toast.remove();
            const contact = allContacts.find(c => c.phone === notif.phone);
            if (contact) selectContact(contact.contact_id || '', notif.phone, contact.display_name || notif.phone, contact.canal_origen || 'whatsapp');
        });
    }

    container.appendChild(toast);
    requestAnimationFrame(() => {
        toast.style.opacity = '1';
        toast.style.transform = 'translateX(0)';
    });

    // Auto-dismiss después de 6 segundos
    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateX(20px)';
        setTimeout(() => toast.remove(), 300);
    }, 6000);
}

function renderNotifList(notifications) {
    const list = document.getElementById('notifList');
    const empty = document.getElementById('notifEmpty');
    if (!list) return;

    // Limpiar items anteriores (preservar el empty placeholder)
    Array.from(list.querySelectorAll('[data-notif-item]')).forEach(el => el.remove());

    if (!notifications.length) {
        if (empty) empty.classList.remove('hidden');
        return;
    }
    if (empty) empty.classList.add('hidden');

    for (const notif of notifications) {
        const isInactivity = notif.type === 'inactivity_24h';
        const isAprobados  = notif.type === 'aprobados_daily';
        const icon  = isInactivity ? '⏰' : isAprobados ? '📢' : '🔔';
        const title = isInactivity
            ? (notif.contact_name || notif.phone || 'Contacto')
            : isAprobados ? 'Contactos aprobados' : 'Notificación';
        const sub   = isInactivity
            ? `Lleva ${notif.hours_waiting || '+24'}h esperando respuesta`
            : (notif.message || '');

        const item = document.createElement('div');
        item.dataset.notifItem = notif.id;
        item.className = 'flex items-start gap-3 px-4 py-3 hover:bg-gray-50 transition-colors' + (isInactivity ? ' cursor-pointer' : '');
        item.innerHTML = `
            <span class="text-base mt-0.5">${icon}</span>
            <div class="flex-1 min-w-0">
                <p class="text-sm font-medium text-gray-800 truncate">${title}</p>
                <p class="text-xs text-gray-500 mt-0.5">${sub}</p>
            </div>
            <button onclick="event.stopPropagation(); dismissNotif('${notif.id}')"
                    class="flex-shrink-0 text-gray-300 hover:text-gray-500 text-xs p-1" title="Descartar">✕</button>
        `;

        if (isInactivity && notif.phone) {
            item.addEventListener('click', (e) => {
                if (e.target.tagName === 'BUTTON') return;
                toggleNotifDropdown(false);
                const contact = allContacts.find(c => c.phone === notif.phone);
                if (contact) selectContact(contact.contact_id || '', notif.phone, contact.display_name || notif.phone, contact.canal_origen || 'whatsapp');
            });
        }

        list.appendChild(item);
    }
}

function toggleNotifDropdown(forceState) {
    const dropdown = document.getElementById('notifDropdown');
    const btn      = document.getElementById('notifBtn');
    if (!dropdown) return;

    _notifDropdownOpen = forceState !== undefined ? forceState : !_notifDropdownOpen;

    if (_notifDropdownOpen && btn) {
        // Calcular posición absoluta en viewport (position:fixed escapa overflow-hidden del sidebar)
        const rect = btn.getBoundingClientRect();
        const dropW = 300;
        // Colocar debajo del botón; ajustar si se sale por la derecha
        let left = rect.left;
        if (left + dropW > window.innerWidth - 8) {
            left = window.innerWidth - dropW - 8;
        }
        dropdown.style.top  = `${rect.bottom + 6}px`;
        dropdown.style.left = `${left}px`;
        dropdown.classList.remove('hidden');
    } else {
        dropdown.classList.add('hidden');
    }
}

// Cerrar dropdown al hacer clic fuera
document.addEventListener('click', (e) => {
    if (_notifDropdownOpen && !e.target.closest('#notifBtnWrapper') && !e.target.closest('#notifDropdown')) {
        toggleNotifDropdown(false);
    }
});

async function dismissNotif(notifId) {
    if (!ADVISOR_ID || !notifId) return;
    try {
        await fetch(`${BASE_URL}/notifications/${encodeURIComponent(notifId)}/read`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-API-Key': API_KEY },
            body: JSON.stringify({ advisor: ADVISOR_ID })
        });
        _seenNotifIds.delete(notifId);
        await pollNotifications(); // Refrescar inmediatamente
    } catch (e) {
        console.warn('[Notif] Error descartando notificación:', e);
    }
}

async function markAllNotifsRead() {
    if (!ADVISOR_ID) return;
    try {
        await fetch(`${BASE_URL}/notifications/read-all`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-API-Key': API_KEY },
            body: JSON.stringify({ advisor: ADVISOR_ID })
        });
        _seenNotifIds = new Set();
        await pollNotifications();
    } catch (e) {
        console.warn('[Notif] Error marcando todo leído:', e);
    }
}

// ── Canal display: permite al asesor cambiar el canal visible del contacto ───

async function updateCanalDisplay(canalValue, canal) {
    if (!currentPhone) return;
    const canalMeta = canal || currentCanal || 'whatsapp';
    try {
        const res = await fetch(`${BASE_URL}/contacts/${encodeURIComponent(currentPhone)}/canal`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json', 'X-API-Key': API_KEY },
            body: JSON.stringify({ canal_display: canalValue, canal: canalMeta })
        });
        if (res.ok) {
            // Actualizar en allContacts para que el fingerprint se mantenga coherente
            const idx = allContacts.findIndex(c => c.phone === currentPhone);
            if (idx !== -1) allContacts[idx].canal_display = canalValue;
            const labelMap = Object.fromEntries(_CANAL_OPTIONS.map(o => [o.value, o.label]));
            showToast(`Canal cambiado a ${labelMap[canalValue] || canalValue}`, 'success');
        } else {
            showToast('Error al actualizar canal', 'error');
        }
    } catch (e) {
        showToast('Error de conexión', 'error');
    }
}

// ======================================================
// checkSilentBotContacts: Safety net de 30 minutos para contactos BOT_ACTIVE sin nombre.
// Cubre el caso donde la notificación inicial fue perdida (contacto nuevo de portal,
// Fix-B probation, etc.) y el cliente nunca dio su nombre tras la pregunta de Sofia.
// Se llama cada 5 minutos para detectar contactos BOT_ACTIVE sin nombre.
function checkSilentBotContacts() {
    const SILENT_THRESHOLD_MS = 30 * 60 * 1000; // 30 min sin respuesta
    const COOLDOWN_MS         = 2 * 60 * 60 * 1000; // 2 horas entre re-notificaciones
    const PHONE_RE = /^\+?[\d\s\-()+]{7,}$/;   // Detecta display_name = teléfono

    if (!allContacts || !allContacts.length) return;

    const now = Date.now();
    let beepCount = 0;
    let refreshNeeded = false;

    let silentShown = {};
    try { silentShown = JSON.parse(sessionStorage.getItem('_silentBotShown') || '{}'); } catch {}

    for (const contact of allContacts) {
        const phone = contact.phone;
        if (!phone) continue;

        // Solo BOT_ACTIVE (Sofia está atendiendo, asesor no ha tomado control)
        const status = contact.conversation_status || contact.status || '';
        if (status !== 'BOT_ACTIVE') continue;

        // No notificar si el chat está abierto ahora mismo
        if (phone === currentPhone) continue;

        // Ya tiene badge — no duplicar
        if (contact.has_unread || (unreadCounts[phone] > 0)) continue;

        // display_name es el teléfono (sin nombre real) — ej: "+573246895528"
        const dn = contact.display_name || '';
        const hasNoRealName = !dn || dn === phone || PHONE_RE.test(dn);
        if (!hasNoRealName) continue;

        // Esperar al menos 30 min desde el último mensaje del cliente
        const lastActivityTs = contact.last_activity
            ? new Date(contact.last_activity).getTime() : 0;
        if (!lastActivityTs || (now - lastActivityTs) < SILENT_THRESHOLD_MS) continue;

        // Cooldown 2 horas por contacto (sessionStorage persiste entre tabs)
        if (silentShown[phone] && (now - silentShown[phone]) < COOLDOWN_MS) continue;

        // ── Disparar notificación ──
        unreadCounts[phone] = 1;
        updateUnreadBadge(phone, 1);
        refreshNeeded = true;

        // Sonido: máx 3 beeps por invocación para no bombardear
        if (beepCount < 3) {
            playNotificationBeep();
            beepCount++;
        }

        // Registrar cooldown
        silentShown[phone] = now;
        try { sessionStorage.setItem('_silentBotShown', JSON.stringify(silentShown)); } catch {}

        console.log('[Panel][SilentBot] Notificación deferred — BOT_ACTIVE sin nombre 30min+:', phone);
    }

    // Forzar loadContacts para que el backend actualice has_unread=True → 3-tier sort → índice 0
    if (refreshNeeded) {
        scheduleContactsRefresh();
    }
}

// ======================================================

function playNotificationBeep() {
    try {
        if (!_sharedAudioCtx) {
            _sharedAudioCtx = new AudioContext();
            console.log('[Panel][Sound] AudioContext creado. Estado:', _sharedAudioCtx.state);
        }
        if (_sharedAudioCtx.state === 'suspended') {
            console.warn('[Panel][Sound] AudioContext suspendido — intentando resume...');
            _sharedAudioCtx.resume().catch(e => console.error('[Panel][Sound] resume() falló:', e));
        }
        const oscillator = _sharedAudioCtx.createOscillator();
        const gainNode = _sharedAudioCtx.createGain();
        oscillator.connect(gainNode);
        gainNode.connect(_sharedAudioCtx.destination);
        oscillator.type = 'sine';
        oscillator.frequency.setValueAtTime(880, _sharedAudioCtx.currentTime);
        gainNode.gain.setValueAtTime(0.25, _sharedAudioCtx.currentTime);
        gainNode.gain.exponentialRampToValueAtTime(0.001, _sharedAudioCtx.currentTime + 0.3);
        oscillator.start(_sharedAudioCtx.currentTime);
        oscillator.stop(_sharedAudioCtx.currentTime + 0.3);
        console.log('[Panel][Sound] Beep reproducido OK');
    } catch(e) {
        console.error('[Panel][Sound] Error al reproducir beep:', e);
    }
}

/**
 * Muestra notificacion del navegador.
 */
function showBrowserNotification(title, body) {
    // Nota: playNotificationBeep() ya se llama en handleNewMessageNotification — no llamar aquí para evitar doble beep
    console.log('[Panel][Notif] Permiso actual:', Notification.permission, '| título:', title);

    // Verificar si las notificaciones estan soportadas y permitidas
    if (!('Notification' in window)) {
        console.warn('[Panel][Notif] Notification API no soportada en este browser');
        return;
    }

    if (Notification.permission === 'granted') {
        new Notification(title, {
            body: body,
            icon: 'https://ui-avatars.com/api/?name=P&background=10B981&color=fff&size=64',
            tag: 'panel-notification'  // Evita multiples notificaciones
        });
        console.log('[Panel][Notif] Notificación mostrada para:', title);
    } else if (Notification.permission === 'denied') {
        console.warn('[Panel][Notif] Permiso denegado — notificación visual no mostrada');
    } else {
        // Pedir permiso
        Notification.requestPermission().then(permission => {
            if (permission === 'granted') {
                showBrowserNotification(title, body);
            }
        });
    }
}

/**
 * Envia ping al WebSocket para mantener la conexion viva.
 */
function sendWebSocketPing() {
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'ping' }));
    }
}

// Iniciar WebSocket cuando el DOM este listo
document.addEventListener('DOMContentLoaded', () => {
    // Restaurar recentlyClosedPhones desde sessionStorage (sobrevive F5)
    // Evita que contactos cerrados recientemente reaparezcan con badge al recargar
    try {
        const _stored = JSON.parse(sessionStorage.getItem('_rcpPhones') || '[]');
        const _now0 = Date.now();
        const _valid = _stored.filter(e => _now0 - e.ts < 90000);
        _valid.forEach(e => {
            recentlyClosedPhones.add(e.phone);
            const remaining = 90000 - (_now0 - e.ts);
            setTimeout(() => recentlyClosedPhones.delete(e.phone), remaining);
        });
        // Limpiar entradas expiradas del storage
        sessionStorage.setItem('_rcpPhones', JSON.stringify(_valid));
        if (_valid.length > 0) console.log('[Panel] Restaurados', _valid.length, 'contactos cerrados desde sessionStorage');
    } catch(_e) {}

    // Conectar WebSocket (con delay para dar tiempo a que el servidor este listo)
    setTimeout(connectWebSocket, 1000);

    // Ping cada 15 segundos para mantener conexion viva (Railway cierra conexiones idle)
    setInterval(sendWebSocketPing, 15000);

    // Pedir permiso para notificaciones
    if ('Notification' in window && Notification.permission === 'default') {
        // Pedir permiso despues de interaccion del usuario
        document.body.addEventListener('click', () => {
            Notification.requestPermission();
        }, { once: true });
    }
});

// =========================================================================
// FUNCIONES DE CREACION MANUAL DE CONTACTOS
// =========================================================================

/**
 * Abre el modal de creacion de contacto.
 */
function openCreateContactModal() {
    const modal = document.getElementById('createContactModal');
    if (modal) {
        modal.classList.remove('hidden');
        // Limpiar formulario y asegurar que sea visible (puede haberse ocultado por 409)
        const form = document.getElementById('createContactForm');
        if (form) { form.reset(); form.classList.remove('hidden'); }
        // Poblar portales según el asesor activo
        const sel = document.getElementById('portalOrigenSelect');
        if (sel) {
            const portals = ADVISOR_PORTALS[ADVISOR_ID] || ALL_PORTALS;
            sel.innerHTML = '<option value="">-- Seleccionar --</option>'
                + portals.map(p => `<option value="${p.value}">${p.label}</option>`).join('');
        }
        // Ocultar mensajes previos
        const resultDiv = document.getElementById('createContactResult');
        if (resultDiv) resultDiv.classList.add('hidden');
    }
}

/**
 * Cierra el modal de creacion de contacto.
 */
function closeCreateContactModal() {
    const modal = document.getElementById('createContactModal');
    if (modal) modal.classList.add('hidden');
}

/**
 * Crea un contacto manualmente desde el formulario del modal.
 * @param {Event} event - Evento submit del formulario
 */
async function createManualContact(event) {
    event.preventDefault();
    console.log('[Panel] createManualContact() iniciado');

    const form = event.target;
    const formData = new FormData(form);
    const submitBtn = document.getElementById('createContactBtn');
    const resultDiv = document.getElementById('createContactResult');
    const resultContent = resultDiv.querySelector('div');

    // Validar campos obligatorios
    const firstname = formData.get('firstname')?.trim();
    const phone = formData.get('phone')?.trim();

    if (!firstname || !phone) {
        showCreateResult('error', 'Nombre y telefono son obligatorios');
        return;
    }

    // Deshabilitar boton mientras procesa
    if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.textContent = 'Creando...';
    }

    try {
        // Enviar advisor_id del asesor que crea el contacto (para asignación directa)
        if (ADVISOR_ID) {
            formData.append('advisor_id', ADVISOR_ID);
        }

        const response = await fetch(`${BASE_URL}/contacts/create`, {
            method: 'POST',
            headers: { 'X-API-Key': API_KEY },
            body: formData
        });

        const data = await response.json();
        console.log('[Panel] Respuesta creacion:', data);

        if (response.status === 409) {
            // Contacto ya existe — ocultar formulario y mostrar panel de info
            const createForm = document.getElementById('createContactForm');
            if (createForm) createForm.classList.add('hidden');

            const ownerLine = (data.owner_name && data.owner_name !== 'Sin asignar')
                ? `<div class="flex justify-between"><span class="text-gray-500">Asesor asignado</span><span class="font-medium">${data.owner_name}</span></div>`
                : `<div class="flex justify-between"><span class="text-gray-500">Asesor</span><span class="text-yellow-600 font-medium">Sin asignar</span></div>`;

            const historyLine = `<div class="flex justify-between"><span class="text-gray-500">Mensajes</span><span class="font-medium">${data.message_count} mensaje${data.message_count !== 1 ? 's' : ''}</span></div>`;

            const canalLine = data.redis_canal
                ? `<div class="flex justify-between"><span class="text-gray-500">Activo en panel</span><span class="text-green-600 font-medium">Sí (${data.redis_canal})</span></div>`
                : '';

            // Canal a usar: activo en Redis → seleccionado en formulario → fallback
            const canalParaTakeControl = data.redis_canal
                || formData.get('canal')
                || 'whatsapp_directo';

            const actionBtn = data.can_take_control
                ? `<button onclick="takeControlOfExisting('${data.contact_id}', '${data.phone}', '${canalParaTakeControl}')"
                       class="px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700 text-sm font-medium">
                       Tomar Control
                   </button>`
                : `<button onclick="requestTransfer('${data.contact_id}', '${data.phone}', '${data.owner_id}', '${data.display_name}', '${canalParaTakeControl}')"
                       class="px-4 py-2 bg-yellow-500 text-white rounded hover:bg-yellow-600 text-sm font-medium">
                       Tomar contacto
                   </button>`;

            showCreateResult('warning', `
                <div class="space-y-2 text-sm">
                    <p class="font-semibold text-yellow-800">âš  Contacto ya existe</p>
                    <div class="bg-white rounded p-2 space-y-1 border border-yellow-200">
                        <div class="flex justify-between"><span class="text-gray-500">Nombre</span><span class="font-medium">${data.display_name}</span></div>
                        <div class="flex justify-between"><span class="text-gray-500">Teléfono</span><span class="font-mono text-xs">${data.phone}</span></div>
                        ${ownerLine}
                        ${historyLine}
                        ${canalLine}
                    </div>
                    <div class="flex gap-2 pt-1">
                        <button onclick="closeCreateContactModal()" class="px-4 py-2 bg-gray-200 rounded hover:bg-gray-300 text-sm">Cancelar</button>
                        ${actionBtn}
                    </div>
                </div>
            `);
        } else if (response.ok && data.status === 'success') {
            // Exito
            showCreateResult('success', `Contacto "${data.display_name}" creado exitosamente`);

            // Cerrar modal despues de 2 segundos y refrescar lista
            setTimeout(() => {
                closeCreateContactModal();
                loadContacts();

                // Seleccionar el nuevo contacto automaticamente
                if (data.contact_id && data.phone) {
                    // Inicializar badge de no leídos en 0
                    unreadCounts[data.phone] = 0;
                    updateUnreadBadge(data.phone, 0);
                    const selectedCanal = formData.get('canal') || 'whatsapp_directo';
                    selectContact(data.contact_id, data.phone, data.display_name, selectedCanal);
                }
            }, 1500);
        } else {
            throw new Error(data.detail || data.message || 'Error desconocido');
        }

    } catch (error) {
        console.error('[Panel] Error creando contacto:', error);
        showCreateResult('error', `Error: ${error.message}`);
    } finally {
        if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.textContent = 'Crear Contacto';
        }
    }
}

/**
 * Muestra resultado de la creacion en el modal.
 * @param {string} type - Tipo: 'success', 'error', 'warning'
 * @param {string} message - Mensaje a mostrar
 */
function showCreateResult(type, message) {
    const resultDiv = document.getElementById('createContactResult');
    const resultContent = resultDiv.querySelector('div');

    const colors = {
        success: 'bg-green-100 text-green-800 border border-green-200',
        error: 'bg-red-100 text-red-800 border border-red-200',
        warning: 'bg-yellow-100 text-yellow-800 border border-yellow-200'
    };

    resultContent.className = `p-3 rounded text-sm ${colors[type] || colors.error}`;
    resultContent.innerHTML = message;
    resultDiv.classList.remove('hidden');
}

// ─── Sistema de Transferencia ────────────────────────────────────────────────

/**
 * Solicita la transferencia de un contacto al asesor propietario.
 */
async function requestTransfer(contactId, phone, ownerAdvisorId, contactName, canal = 'whatsapp_directo') {
    const url = `${BASE_URL}/contacts/${encodeURIComponent(contactId)}/transfer-request?` +
        `phone=${encodeURIComponent(phone)}&` +
        `requesting_advisor_id=${encodeURIComponent(ADVISOR_ID || '')}&` +
        `owner_advisor_id=${encodeURIComponent(ownerAdvisorId)}&` +
        `canal=${encodeURIComponent(canal)}&` +
        `contact_name=${encodeURIComponent(contactName || phone)}`;

    try {
        const resp = await fetch(url, { method: 'POST', headers: { 'X-API-Key': API_KEY } });
        const data = await resp.json();
        if (data.status === 'transferred') {
            // Transferencia inmediata — cerrar modal y refrescar panel
            closeCreateContactModal();
            scheduleContactsRefresh();
            showToast(`Contacto transferido — aparecerá en tu panel`, 'success');
        } else {
            showCreateResult('error', 'Error al tomar contacto');
        }
    } catch (e) {
        console.error('[Panel] Error en requestTransfer:', e);
        showCreateResult('error', 'Error de conexión');
    }
}

/**
 * Muestra modal flotante al asesor propietario cuando recibe una solicitud de transferencia.
 */
function showTransferRequestModal(msg) {
    // Remover modal anterior si existe
    document.getElementById('transferRequestModal')?.remove();

    const div = document.createElement('div');
    div.id = 'transferRequestModal';
    div.className = 'fixed bottom-4 right-4 bg-white shadow-xl rounded-lg p-4 border border-yellow-300 z-50 max-w-sm';
    div.innerHTML = `
        <div class="flex items-start gap-2">
            <span class="text-yellow-500 text-lg mt-0.5">📤</span>
            <div class="flex-1">
                <p class="font-semibold text-sm text-yellow-800">Solicitud de transferencia</p>
                <p class="text-sm text-gray-700 mt-1">
                    <strong>${msg.requester_name || 'Un asesor'}</strong> quiere atender a
                    <strong>${msg.contact_name || msg.phone}</strong>
                </p>
                <div class="flex gap-2 mt-3">
                    <button onclick="acceptTransfer('${msg.contact_id}')"
                        class="px-3 py-1.5 bg-[#F5C400] text-[#1A1A1A] text-xs rounded hover:bg-[#D4A800] font-semibold">
                        Aceptar
                    </button>
                    <button onclick="rejectTransfer('${msg.contact_id}')"
                        class="px-3 py-1.5 bg-red-500 text-white text-xs rounded hover:bg-red-600 font-medium">
                        Rechazar
                    </button>
                    <button onclick="document.getElementById('transferRequestModal')?.remove()"
                        class="px-3 py-1.5 bg-gray-200 text-gray-600 text-xs rounded hover:bg-gray-300">
                        Ignorar
                    </button>
                </div>
            </div>
        </div>
    `;
    document.body.appendChild(div);
}

/**
 * Acepta una solicitud de transferencia (llamado por el asesor propietario).
 */
async function acceptTransfer(contactId) {
    document.getElementById('transferRequestModal')?.remove();
    try {
        await fetch(
            `${BASE_URL}/contacts/${encodeURIComponent(contactId)}/transfer-accept?by_advisor_id=${encodeURIComponent(ADVISOR_ID || '')}`,
            { method: 'POST', headers: { 'X-API-Key': API_KEY } }
        );
    } catch (e) {
        console.error('[Panel] Error en acceptTransfer:', e);
    }
}

/**
 * Rechaza una solicitud de transferencia (llamado por el asesor propietario).
 */
async function rejectTransfer(contactId) {
    document.getElementById('transferRequestModal')?.remove();
    try {
        await fetch(
            `${BASE_URL}/contacts/${encodeURIComponent(contactId)}/transfer-reject?by_advisor_id=${encodeURIComponent(ADVISOR_ID || '')}`,
            { method: 'POST', headers: { 'X-API-Key': API_KEY } }
        );
    } catch (e) {
        console.error('[Panel] Error en rejectTransfer:', e);
    }
}

/**
 * Muestra una notificación tipo toast temporal.
 * @param {string} message - Texto a mostrar
 * @param {'success'|'warning'|'error'|'info'} type - Tipo de notificación
 */
function showToast(message, type = 'info') {
    const cssClass = {
        success: 'toast-success',
        warning: 'toast-warning',
        error:   'toast-error',
        info:    'toast-info'
    }[type] || 'toast-info';
    const div = document.createElement('div');
    div.className = `fixed top-4 right-4 z-50 px-4 py-3 rounded-lg text-sm font-medium ${cssClass}`;
    div.textContent = message;
    document.body.appendChild(div);
    setTimeout(() => {
        div.style.transition = 'opacity 0.3s';
        div.style.opacity = '0';
        setTimeout(() => div.remove(), 300);
    }, 3700);
}

// ─────────────────────────────────────────────────────────────────────────────

/**
 * Toma control de un contacto existente (cuando se detecta duplicado).
 * @param {string} contactId - ID del contacto en HubSpot
 * @param {string} phone - Telefono normalizado
 */
async function takeControlOfExisting(contactId, phone, canal = 'whatsapp_directo') {
    console.log('[Panel] Tomando control de contacto existente:', contactId, phone, canal);

    try {
        // Usar el endpoint de take-control existente
        const takeControlUrl = `${BASE_URL}/contacts/${encodeURIComponent(phone)}/take-control?` +
            `canal=${encodeURIComponent(canal)}&contact_id=${encodeURIComponent(contactId)}` +
            (ADVISOR_ID ? `&advisor_id=${encodeURIComponent(ADVISOR_ID)}` : '');

        const response = await fetch(takeControlUrl, {
            method: 'POST',
            headers: { 'X-API-Key': API_KEY }
        });

        const data = await response.json();
        console.log('[Panel] Take control response:', data);

        if (data.status === 'success') {
            showCreateResult('success', 'Control tomado exitosamente');

            // Cerrar modal y refrescar
            setTimeout(() => {
                closeCreateContactModal();
                loadContacts();
            }, 1000);
        } else {
            throw new Error(data.detail || 'Error tomando control');
        }

    } catch (error) {
        console.error('[Panel] Error tomando control:', error);
        showCreateResult('error', `Error: ${error.message}`);
    }
}

// =========================================================================
// FUNCIONES DE TRANSFERENCIA DE CONTACTOS
// =========================================================================

let advisorsList = [];       // Cache de asesores
let _advisorsCacheTime = 0;  // Timestamp de última carga (ms)
const ADVISORS_CACHE_TTL = 5 * 60 * 1000;  // 5 minutos

/**
 * Abre el modal de transferencia de contacto.
 */
async function openTransferModal() {
    if (!currentPhone || !currentContactId) {
        alert('Selecciona un contacto primero');
        return;
    }

    const modal = document.getElementById('transferContactModal');
    if (modal) {
        modal.classList.remove('hidden');

        // Rellenar info del contacto
        document.getElementById('transferContactName').textContent = currentName || 'Sin nombre';
        document.getElementById('transferContactPhone').textContent = currentPhone;
        document.getElementById('transferPhone').value = currentPhone;
        document.getElementById('transferContactId').value = currentContactId;
        document.getElementById('transferCanal').value = currentCanal || 'whatsapp';

        // Ocultar mensajes previos
        const resultDiv = document.getElementById('transferResult');
        if (resultDiv) resultDiv.classList.add('hidden');

        // Cargar lista de asesores
        await loadAdvisorsList();
    }
}

/**
 * Cierra el modal de transferencia.
 */
function closeTransferModal() {
    const modal = document.getElementById('transferContactModal');
    if (modal) modal.classList.add('hidden');
}

/**
 * Carga la lista de asesores disponibles para transferencia.
 * Incluye timeout de 5s y retry automático.
 */
async function loadAdvisorsList() {
    const select = document.getElementById('transferToOwner');
    if (!select) return;

    // Usar caché si está fresca (< 5 min) y hay datos
    const now = Date.now();
    if (advisorsList.length > 0 && (now - _advisorsCacheTime) < ADVISORS_CACHE_TTL) {
        console.log('[Panel] Asesores desde caché:', advisorsList.length);
        _populateAdvisorsSelect(select, advisorsList);
        return;
    }

    // Mostrar cargando
    select.innerHTML = '<option value="">Cargando asesores...</option>';

    const maxRetries = 2;
    let lastError = null;

    for (let attempt = 1; attempt <= maxRetries; attempt++) {
        try {
            // Timeout de 5 segundos
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), 5000);

            const response = await fetch(`${BASE_URL}/advisors`, {
                headers: { 'X-API-Key': API_KEY },
                signal: controller.signal
            });

            clearTimeout(timeoutId);

            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }

            const data = await response.json();
            advisorsList = data.advisors || [];
            _advisorsCacheTime = Date.now();

            _populateAdvisorsSelect(select, advisorsList);
            console.log('[Panel] Asesores cargados y cacheados:', advisorsList.length);
            return; // Éxito, salir

        } catch (error) {
            lastError = error;
            console.warn(`[Panel] Intento ${attempt}/${maxRetries} fallido:`, error.message);

            if (attempt < maxRetries) {
                await new Promise(resolve => setTimeout(resolve, 1000));
            }
        }
    }

    // Todos los intentos fallaron
    console.error('[Panel] Error cargando asesores después de', maxRetries, 'intentos:', lastError);
    select.innerHTML = '<option value="">Error - Reintentar más tarde</option>';
}

function _populateAdvisorsSelect(select, advisors) {
    select.innerHTML = '<option value="">-- Seleccionar asesora --</option>';
    for (const advisor of advisors) {
        const option = document.createElement('option');
        option.value = advisor.id;
        option.textContent = advisor.name;
        select.appendChild(option);
    }
}

/** Invalida el caché de asesores (llamar tras crear/eliminar una asesora). */
function invalidateAdvisorsCache() {
    advisorsList = [];
    _advisorsCacheTime = 0;
}

/**
 * Ejecuta la transferencia del contacto.
 * @param {Event} event - Evento submit del formulario
 */
async function transferContact(event) {
    event.preventDefault();
    console.log('[Panel] transferContact() iniciado');

    const form = event.target;
    const formData = new FormData(form);
    const submitBtn = document.getElementById('transferBtn');

    const toOwnerId = formData.get('to_owner_id');
    if (!toOwnerId) {
        showTransferResult('error', 'Selecciona una asesora destino');
        return;
    }

    // Deshabilitar boton mientras procesa
    if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.textContent = 'Transfiriendo...';
    }

    try {
        const phone = formData.get('phone');
        const response = await fetch(`${BASE_URL}/contacts/${encodeURIComponent(phone)}/transfer`, {
            method: 'POST',
            headers: { 'X-API-Key': API_KEY },
            body: formData
        });

        const data = await response.json();
        console.log('[Panel] Respuesta transferencia:', data);

        if (response.ok && data.status === 'success') {
            // Obtener nombre del asesor destino
            const toAdvisor = advisorsList.find(a => a.id === toOwnerId);
            const toName = toAdvisor ? toAdvisor.name : toOwnerId;

            showTransferResult('success', `Contacto transferido a ${toName}`);

            // Cerrar modal y refrescar
            setTimeout(() => {
                closeTransferModal();
                loadContacts();
            }, 1500);
        } else {
            throw new Error(data.detail || data.message || 'Error en transferencia');
        }

    } catch (error) {
        console.error('[Panel] Error en transferencia:', error);
        showTransferResult('error', `Error: ${error.message}`);
    } finally {
        if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.textContent = 'Transferir';
        }
    }
}

/**
 * Muestra resultado de la transferencia en el modal.
 * @param {string} type - Tipo: 'success', 'error', 'warning'
 * @param {string} message - Mensaje a mostrar
 */
function showTransferResult(type, message) {
    const resultDiv = document.getElementById('transferResult');
    const resultContent = resultDiv.querySelector('div');

    const colors = {
        success: 'bg-green-100 text-green-800 border border-green-200',
        error: 'bg-red-100 text-red-800 border border-red-200',
        warning: 'bg-yellow-100 text-yellow-800 border border-yellow-200'
    };

    resultContent.className = `p-3 rounded text-sm ${colors[type] || colors.error}`;
    resultContent.textContent = message;
    resultDiv.classList.remove('hidden');
}

// =========================================================================
// WORKERS — Gestión del equipo de campo
// =========================================================================

let workersCache = [];       // Cache local de workers para el selector del modal de citas
let _workersCacheTime = 0;  // Timestamp de última carga (ms)
const WORKERS_CACHE_TTL = 5 * 60 * 1000;  // 5 minutos

async function openWorkersModal() {
    document.getElementById('workersModal').classList.remove('hidden');

    // Mostrar sección de nombre del asesor si hay ADVISOR_ID
    if (ADVISOR_ID) {
        document.getElementById('advisorNameSection').classList.remove('hidden');
        document.getElementById('advisorSeparator').classList.remove('hidden');
        document.getElementById('advisorNameInput').value = ADVISOR_NAME || '';
        document.getElementById('advisorNameStatus').classList.add('hidden');
    }

    await loadWorkers();
}

function closeWorkersModal() {
    document.getElementById('workersModal').classList.add('hidden');
    document.getElementById('newWorkerName').value = '';
    document.getElementById('advisorNameStatus').classList.add('hidden');
}

/**
 * Guarda el nuevo nombre del asesor en MongoDB y actualiza el título del panel.
 */
async function saveAdvisorName() {
    const input = document.getElementById('advisorNameInput');
    const statusEl = document.getElementById('advisorNameStatus');
    const newName = input.value.trim();

    if (!newName) {
        input.focus();
        return;
    }

    if (!ADVISOR_ID) {
        statusEl.textContent = 'Error: No hay ID de asesor';
        statusEl.className = 'text-xs mt-1 text-red-500';
        statusEl.classList.remove('hidden');
        return;
    }

    try {
        const response = await fetch(`${BASE_URL}/advisors/${ADVISOR_ID}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json', 'X-API-Key': API_KEY },
            body: JSON.stringify({ name: newName })
        });

        if (response.ok) {
            // Actualizar variables globales y título del panel
            ADVISOR_NAMES[ADVISOR_ID] = newName;
            // No podemos reasignar ADVISOR_NAME (const), pero actualizamos el DOM directamente
            document.getElementById('panelTitle').textContent = `Panel de ${newName}`;

            // Actualizar también la lista de advisors para el dropdown de transferencias
            const advisorIndex = advisorsList.findIndex(a => a.id === ADVISOR_ID);
            if (advisorIndex >= 0) {
                advisorsList[advisorIndex].name = newName;
            }

            statusEl.textContent = '✓ Nombre actualizado correctamente';
            statusEl.className = 'text-xs mt-1 text-green-600';
            statusEl.classList.remove('hidden');

            console.log(`[Panel] Nombre del asesor actualizado a: ${newName}`);
        } else {
            const err = await response.json();
            statusEl.textContent = 'Error: ' + (err.detail || 'No se pudo actualizar');
            statusEl.className = 'text-xs mt-1 text-red-500';
            statusEl.classList.remove('hidden');
        }
    } catch (e) {
        statusEl.textContent = 'Error de conexión al guardar';
        statusEl.className = 'text-xs mt-1 text-red-500';
        statusEl.classList.remove('hidden');
        console.error('[Panel] Error guardando nombre del asesor:', e);
    }
}

async function loadWorkers(forceRefresh = false) {
    // Usar caché si está fresca (< 5 min) y hay datos
    const now = Date.now();
    if (!forceRefresh && workersCache.length > 0 && (now - _workersCacheTime) < WORKERS_CACHE_TTL) {
        console.log('[Panel] Workers desde caché:', workersCache.length);
        renderWorkersList(workersCache);
        return;
    }

    try {
        const response = await fetch(`${BASE_URL}/workers`, {
            headers: { 'X-API-Key': API_KEY }
        });
        const data = await response.json();
        workersCache = data.workers || [];
        _workersCacheTime = Date.now();
        renderWorkersList(workersCache);
    } catch (err) {
        console.error('[Panel] Error cargando workers:', err);
        document.getElementById('workersList').innerHTML =
            '<p class="text-sm text-red-500 text-center py-2">Error cargando encargados</p>';
    }
}

function renderWorkersList(workers) {
    const container = document.getElementById('workersList');
    if (!workers || workers.length === 0) {
        container.innerHTML = '<p class="text-sm text-gray-400 text-center py-4">Sin encargados aún. Agrega el primero.</p>';
        return;
    }
    container.innerHTML = workers.map(w => `
        <div class="flex items-center gap-2 p-2 bg-gray-50 rounded border" data-worker-id="${w.id}">
            <span class="flex-1 text-sm font-medium text-gray-700 worker-name-display">${w.name}</span>
            <input type="text" value="${w.name}"
                class="hidden flex-1 text-sm border rounded px-2 py-1 focus:ring-1 focus:ring-yellow-400 worker-name-input">
            <button onclick="startEditWorker('${w.id}')"
                class="text-blue-500 hover:text-blue-700 text-xs px-2 py-1 worker-edit-btn"
                title="Editar nombre">&#9998;</button>
            <button onclick="saveEditWorker('${w.id}')"
                class="hidden text-yellow-600 hover:text-yellow-800 text-xs px-2 py-1 worker-save-btn"
                title="Guardar">&#10003;</button>
            <button onclick="deleteWorker('${w.id}', '${w.name}')"
                class="text-red-400 hover:text-red-600 text-xs px-2 py-1"
                title="Eliminar encargado">&#10005;</button>
        </div>
    `).join('');
}

function startEditWorker(workerId) {
    const row = document.querySelector(`[data-worker-id="${workerId}"]`);
    if (!row) return;
    row.querySelector('.worker-name-display').classList.add('hidden');
    row.querySelector('.worker-name-input').classList.remove('hidden');
    row.querySelector('.worker-edit-btn').classList.add('hidden');
    row.querySelector('.worker-save-btn').classList.remove('hidden');
    row.querySelector('.worker-name-input').focus();
}

async function saveEditWorker(workerId) {
    const row = document.querySelector(`[data-worker-id="${workerId}"]`);
    if (!row) return;
    const newName = row.querySelector('.worker-name-input').value.trim();
    if (!newName) return;

    try {
        const response = await fetch(`${BASE_URL}/workers/${workerId}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json', 'X-API-Key': API_KEY },
            body: JSON.stringify({ name: newName })
        });
        if (response.ok) {
            await loadWorkers(true); // forceRefresh: invalidar caché tras editar
        } else {
            const err = await response.json();
            alert('Error: ' + (err.detail || 'No se pudo actualizar'));
        }
    } catch (e) {
        alert('Error de conexión al actualizar encargado');
    }
}

async function createWorker() {
    const input = document.getElementById('newWorkerName');
    const name = input.value.trim();
    if (!name) { input.focus(); return; }

    try {
        const response = await fetch(`${BASE_URL}/workers`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-API-Key': API_KEY },
            body: JSON.stringify({ name })
        });
        if (response.ok) {
            input.value = '';
            await loadWorkers(true); // forceRefresh: invalidar caché tras crear
        } else {
            const err = await response.json();
            alert('Error: ' + (err.detail || 'No se pudo crear el encargado'));
        }
    } catch (e) {
        alert('Error de conexión al crear encargado');
    }
}

async function deleteWorker(workerId, workerName) {
    if (!confirm(`¿Eliminar a "${workerName}"? Sus citas históricas se conservan.`)) return;
    try {
        const response = await fetch(`${BASE_URL}/workers/${workerId}`, {
            method: 'DELETE',
            headers: { 'X-API-Key': API_KEY }
        });
        if (response.ok) {
            await loadWorkers(true); // forceRefresh: invalidar caché tras eliminar
        } else {
            const err = await response.json();
            alert('Error: ' + (err.detail || 'No se pudo eliminar'));
        }
    } catch (e) {
        alert('Error de conexión al eliminar encargado');
    }
}

// =========================================================================
// APPOINTMENTS — Gestión de citas
// =========================================================================

let currentAppointments = []; // Cache de citas del contacto actual

async function openAppointmentModal() {
    if (!currentContactId) return;

    // Resetear estado
    document.getElementById('appointmentResult').classList.add('hidden');
    document.getElementById('editingApptId').value = '';
    currentAppointments = [];

    document.getElementById('appointmentModal').classList.remove('hidden');

    // Cargar workers
    await _loadWorkersIntoSelect();

    // Cargar citas y decidir vista
    await _loadAppointmentsAndRender();
}

function closeAppointmentModal() {
    document.getElementById('appointmentModal').classList.add('hidden');
}

async function _loadWorkersIntoSelect() {
    const select = document.getElementById('apptWorkerSelect');
    select.innerHTML = '<option value="">Cargando...</option>';
    try {
        if (workersCache.length === 0) {
            const response = await fetch(`${BASE_URL}/workers`, {
                headers: { 'X-API-Key': API_KEY }
            });
            const data = await response.json();
            workersCache = data.workers || [];
        }
        if (workersCache.length === 0) {
            select.innerHTML = '<option value="">Sin encargados — agrega uno en ⚙️</option>';
            return;
        }
        select.innerHTML = '<option value="">-- Seleccionar encargado --</option>' +
            workersCache.map(w => `<option value="${w.id}" data-name="${w.name}">${w.name}</option>`).join('');
    } catch (e) {
        select.innerHTML = '<option value="">Error cargando encargados</option>';
    }
}

async function _loadAppointmentsAndRender() {
    const listView = document.getElementById('appointmentListView');
    const formView = document.getElementById('appointmentForm');
    const scheduledSection = document.getElementById('scheduledApptsSection');
    const scheduledList = document.getElementById('scheduledApptsList');
    const pastSection = document.getElementById('pastApptsSection');
    const pastList = document.getElementById('pastApptsList');
    const modalTitle = document.getElementById('apptModalTitle');

    try {
        const response = await fetch(`${BASE_URL}/contacts/${currentContactId}/appointments`, {
            headers: { 'X-API-Key': API_KEY }
        });
        const data = await response.json();
        currentAppointments = data.appointments || [];

        const now = new Date();

        // Separar citas programadas (futuras y activas) de pasadas/canceladas
        const scheduled = currentAppointments.filter(a => {
            if (a.status === 'cancelled') return false;
            const dt = a.appointment_dt ? new Date(a.appointment_dt) : null;
            return dt && dt > now;
        }).sort((a, b) => new Date(a.appointment_dt) - new Date(b.appointment_dt));

        const past = currentAppointments.filter(a => {
            if (a.status === 'cancelled') return true;
            const dt = a.appointment_dt ? new Date(a.appointment_dt) : null;
            return !dt || dt <= now;
        }).sort((a, b) => new Date(b.appointment_dt) - new Date(a.appointment_dt));

        // Renderizar citas programadas
        if (scheduled.length > 0) {
            scheduledSection.classList.remove('hidden');
            scheduledList.innerHTML = scheduled.map(a => _renderScheduledAppointment(a)).join('');
            modalTitle.textContent = '📅 Citas';
        } else {
            scheduledSection.classList.add('hidden');
        }

        // Renderizar citas pasadas
        if (past.length > 0) {
            pastSection.classList.remove('hidden');
            pastList.innerHTML = past.map(a => _renderPastAppointment(a)).join('');
        } else {
            pastSection.classList.add('hidden');
        }

        // Mostrar vista de lista, ocultar form
        listView.classList.remove('hidden');
        formView.classList.add('hidden');

        // Si no hay ninguna cita, mostrar form directamente
        if (currentAppointments.length === 0) {
            showAppointmentForm();
        }

    } catch (e) {
        console.error('Error cargando citas:', e);
        // En caso de error, mostrar el formulario
        showAppointmentForm();
    }
}

function _renderScheduledAppointment(appt) {
    const dt = appt.appointment_dt ? new Date(appt.appointment_dt) : null;
    const dateStr = dt ? dt.toLocaleString('es-CO', {
        weekday: 'short', day: 'numeric', month: 'short', year: 'numeric',
        hour: 'numeric', minute: '2-digit', hour12: true
    }) : '—';

    return `
        <div class="bg-amber-50 border border-amber-200 rounded-lg p-3 relative group">
            <div class="flex justify-between items-start">
                <div class="flex-1">
                    <p class="font-medium text-amber-800 text-sm">${dateStr}</p>
                    <p class="text-xs text-gray-600 mt-1">
                        <span class="font-medium">Encargado:</span> ${appt.worker_name || '—'}
                    </p>
                    ${appt.notes ? `<p class="text-xs text-gray-500 mt-1 italic">${appt.notes}</p>` : ''}
                </div>
                <div class="flex gap-1 opacity-70 group-hover:opacity-100">
                    <button onclick="editAppointment('${appt.id}')" 
                        class="p-1.5 text-blue-600 hover:bg-blue-100 rounded transition-colors" title="Editar">
                        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" 
                                d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"/>
                        </svg>
                    </button>
                    <button onclick="deleteAppointment('${appt.id}')" 
                        class="p-1.5 text-red-600 hover:bg-red-100 rounded transition-colors" title="Eliminar">
                        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" 
                                d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/>
                        </svg>
                    </button>
                </div>
            </div>
        </div>
    `;
}

function _renderPastAppointment(appt) {
    const dt = appt.appointment_dt ? new Date(appt.appointment_dt) : null;
    const dateStr = dt ? dt.toLocaleString('es-CO', {
        dateStyle: 'short', timeStyle: 'short'
    }) : '—';
    const isCancelled = appt.status === 'cancelled';
    const statusClass = isCancelled ? 'text-red-400 line-through' : 'text-gray-600';
    const statusBadge = isCancelled ? '<span class="text-red-400 text-xs ml-1">(cancelada)</span>' : '';

    return `<div class="${statusClass}">📅 ${dateStr} — ${appt.worker_name}${appt.notes ? ` | ${appt.notes}` : ''}${statusBadge}</div>`;
}

function showAppointmentForm(isEdit = false) {
    const listView = document.getElementById('appointmentListView');
    const formView = document.getElementById('appointmentForm');
    const submitBtn = document.getElementById('apptSubmitBtn');
    const modalTitle = document.getElementById('apptModalTitle');

    if (!isEdit) {
        // Nueva cita - resetear form
        document.getElementById('editingApptId').value = '';
        document.getElementById('apptWorkerSelect').value = '';
        document.getElementById('apptNotes').value = '';
        submitBtn.textContent = 'Agendar Cita';
        modalTitle.textContent = '📅 Nueva Cita';

        // Fecha por defecto: mañana 10AM
        const tomorrow = new Date();
        tomorrow.setDate(tomorrow.getDate() + 1);
        tomorrow.setHours(10, 0, 0, 0);
        document.getElementById('apptDatetime').value = tomorrow.toISOString().slice(0, 16);
    }

    listView.classList.add('hidden');
    formView.classList.remove('hidden');
    submitBtn.disabled = false;
}

function hideAppointmentForm() {
    // Volver a la vista de lista
    if (currentAppointments.length > 0) {
        document.getElementById('appointmentListView').classList.remove('hidden');
        document.getElementById('appointmentForm').classList.add('hidden');
        document.getElementById('apptModalTitle').textContent = '📅 Citas';
    } else {
        closeAppointmentModal();
    }
}

function editAppointment(apptId) {
    const appt = currentAppointments.find(a => a.id === apptId);
    if (!appt) return;

    document.getElementById('editingApptId').value = apptId;
    document.getElementById('apptModalTitle').textContent = '📅 Editar Cita';
    document.getElementById('apptSubmitBtn').textContent = 'Guardar Cambios';

    // Cargar datos en el form
    const select = document.getElementById('apptWorkerSelect');
    for (let opt of select.options) {
        if (opt.value === appt.worker_id) {
            opt.selected = true;
            break;
        }
    }

    if (appt.appointment_dt) {
        const dt = new Date(appt.appointment_dt);
        const pad = n => String(n).padStart(2, '0');
        document.getElementById('apptDatetime').value =
            `${dt.getFullYear()}-${pad(dt.getMonth() + 1)}-${pad(dt.getDate())}` +
            `T${pad(dt.getHours())}:${pad(dt.getMinutes())}`;
    }

    document.getElementById('apptNotes').value = appt.notes || '';

    showAppointmentForm(true);
}

async function deleteAppointment(apptId) {
    if (!confirm('¿Eliminar esta cita permanentemente?')) return;

    try {
        const response = await fetch(`${BASE_URL}/appointments/${apptId}`, {
            method: 'DELETE',
            headers: { 'X-API-Key': API_KEY }
        });

        if (response.ok) {
            showToast('Cita eliminada', 'success');
            await _loadAppointmentsAndRender();
            loadContacts(); // Actualizar badge
        } else {
            showToast('Error al eliminar cita', 'error');
        }
    } catch (e) {
        showToast('Error de conexión', 'error');
    }
}

async function submitAppointment(event) {
    event.preventDefault();
    if (!currentContactId) return;

    const editingId = document.getElementById('editingApptId').value;
    const select = document.getElementById('apptWorkerSelect');
    const workerId = select.value;
    const workerName = select.options[select.selectedIndex]?.dataset?.name || '';
    const datetimeVal = document.getElementById('apptDatetime').value;
    const notes = document.getElementById('apptNotes').value.trim();

    if (!workerId || !datetimeVal) return;

    const submitBtn = document.getElementById('apptSubmitBtn');
    submitBtn.disabled = true;
    submitBtn.textContent = editingId ? 'Guardando...' : 'Agendando...';

    try {
        let response;
        if (editingId) {
            // Actualizar cita existente
            response = await fetch(`${BASE_URL}/appointments/${editingId}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json', 'X-API-Key': API_KEY },
                body: JSON.stringify({
                    worker_id: workerId,
                    worker_name: workerName,
                    appointment_dt: datetimeVal,
                    notes: notes
                })
            });
        } else {
            // Crear nueva cita
            response = await fetch(`${BASE_URL}/contacts/${currentContactId}/appointments`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-API-Key': API_KEY },
                body: JSON.stringify({
                    worker_id: workerId,
                    worker_name: workerName,
                    appointment_dt: datetimeVal,
                    notes: notes,
                    advisor_id: ADVISOR_ID || null,
                    canal: currentCanal || 'whatsapp'
                })
            });
        }

        const data = await response.json();
        const resultDiv = document.getElementById('appointmentResult');
        const content = resultDiv.querySelector('div');

        if (response.ok) {
            content.className = 'p-3 rounded text-sm bg-green-100 text-green-800 border border-green-200';
            content.innerHTML = editingId
                ? `✅ Cita actualizada correctamente`
                : `✅ Cita agendada con <strong>${workerName}</strong><br><span class="text-xs">${data.fecha_display || ''}</span>`;
            resultDiv.classList.remove('hidden');

            // [Badge] Update optimista: mostrar badge 📅 inmediatamente sin esperar GET /contacts
            // Solo en citas nuevas (no edición). La confirmación del servidor llega con loadContacts().
            if (!editingId && currentContactId) {
                const _badgeTarget = allContacts.find(c => c.contact_id === currentContactId);
                if (_badgeTarget) {
                    _badgeTarget.has_appointment = true;
                    _applyFiltersAndRender();
                    console.log('[Badge] Optimistic update: has_appointment=true para', currentContactId);
                }
            }

            // Recargar y volver a lista (confirma estado real desde servidor)
            setTimeout(async () => {
                await _loadAppointmentsAndRender();
                loadContacts(); // Sincroniza badge con estado real del backend
                resultDiv.classList.add('hidden');
            }, 1500);
        } else {
            content.className = 'p-3 rounded text-sm bg-red-100 text-red-800 border border-red-200';
            content.textContent = 'Error: ' + (data.detail || 'No se pudo guardar la cita');
            resultDiv.classList.remove('hidden');
            submitBtn.disabled = false;
            submitBtn.textContent = editingId ? 'Guardar Cambios' : 'Agendar Cita';
        }
    } catch (err) {
        const resultDiv = document.getElementById('appointmentResult');
        const content = resultDiv.querySelector('div');
        content.className = 'p-3 rounded text-sm bg-red-100 text-red-800 border border-red-200';
        content.textContent = 'Error de conexión. Intenta de nuevo.';
        resultDiv.classList.remove('hidden');
        submitBtn.disabled = false;
        submitBtn.textContent = editingId ? 'Guardar Cambios' : 'Agendar Cita';
    }
}

// =========================================================================
// BULK MESSAGING — Mensaje masivo por embudo (per-asesora)
// =========================================================================

const BULK_EXCLUDED_STAGES = [
    "1326623075",  // En Conversación (stage unificado)
    "evangelist",  // Cerrado perdido
    "1326632628",  // Ana Contratos
    "1326632209"   // Pagos y Servicios Publicos
];

// Plantillas permitidas para envío masivo. La var key='1' (nombre) siempre se
// auto-fill desde HubSpot per-contacto. Las demás se escriben en el modal.
const BULK_TEMPLATES = [
    {
        sid: 'HX550a2475d09a5fb3b5410e6d36eadf3f',
        name: 'aun_en_busqueda',
        label: '¿Aún estás interesado?',
        preview: 'Hola {1} 😊 ¿Aún continúas en la búsqueda de un inmueble para arriendo?',
        vars: [
            { key: '1', label: 'Nombre del contacto', auto_fill: 'firstname' }
        ]
    },
    {
        sid: 'HX287a1c005459a6ceaec90a35108330c3',
        name: 'seguimiento_personalizado',
        label: 'Seguimiento Personalizado',
        preview: 'Hola {1} ¿Como se encuentra el día de hoy? {2}. Estaré pendiente a su respuesta.',
        vars: [
            { key: '1', label: 'Nombre del contacto', auto_fill: 'firstname' },
            { key: '2', label: 'Mensaje personalizado' }
        ]
    }
];

let _bulkPreviewDebounce = null;
let _bulkPollTimer = null;
let _activeBulkCampaignId = null;

function _updateBulkButtonVisibility() {
    const btn = document.getElementById('bulkMessagingBtn');
    if (!btn) return;
    if (activeStageFilter && !BULK_EXCLUDED_STAGES.includes(activeStageFilter)) {
        btn.classList.remove('hidden');
    } else {
        btn.classList.add('hidden');
    }
}

function _formatDateShort(isoStr) {
    if (!isoStr) return '—';
    try {
        const d = new Date(isoStr);
        if (isNaN(d.getTime())) return isoStr.slice(0, 10);
        return d.toLocaleDateString('es-CO', { day: '2-digit', month: '2-digit', year: 'numeric' });
    } catch (_) { return isoStr.slice(0, 10); }
}

async function openBulkMessagingModal() {
    if (!activeStageFilter || BULK_EXCLUDED_STAGES.includes(activeStageFilter)) {
        alert('Selecciona un embudo elegible primero.');
        return;
    }
    if (!ADVISOR_ID) {
        alert('No se detectó tu identificador de asesora. Recarga el panel.');
        return;
    }

    const stageObj = PIPELINE_STAGES.find(s => s.id === activeStageFilter);
    document.getElementById('bulkStageName').textContent = stageObj ? stageObj.name : activeStageFilter;
    document.getElementById('bulkPreview').textContent = 'Cargando contactos...';
    document.getElementById('bulkSendCount').textContent = '0';
    document.getElementById('bulkSendBtn').disabled = true;

    // Fechas: últimos 30 días por default
    const today = new Date();
    const thirtyDaysAgo = new Date(today.getTime() - 30 * 86400000);
    document.getElementById('bulkDateFrom').value = thirtyDaysAgo.toISOString().slice(0, 10);
    document.getElementById('bulkDateTo').value = today.toISOString().slice(0, 10);

    // Cargar las 2 plantillas permitidas para bulk
    _populateBulkTemplateSelect();
    onBulkTemplateChange();  // renderiza vars de la primera plantilla

    // Listeners para preview live (debounce)
    ['bulkDateFrom', 'bulkDateTo'].forEach(id => {
        const el = document.getElementById(id);
        if (el && !el._bulkBound) {
            el.addEventListener('change', _scheduleBulkPreview);
            el._bulkBound = true;
        }
    });

    // Cargar último envío de esta asesora en este embudo
    _loadLastBulkCampaignBanner();

    document.getElementById('bulkMessagingModal').classList.remove('hidden');
    _scheduleBulkPreview();
}

function closeBulkMessagingModal() {
    document.getElementById('bulkMessagingModal').classList.add('hidden');
}

function _populateBulkTemplateSelect() {
    const sel = document.getElementById('bulkTemplateSelect');
    if (!sel) return;
    sel.innerHTML = BULK_TEMPLATES.map(t =>
        `<option value="${t.sid}">${t.label}</option>`
    ).join('');
}

function _getSelectedBulkTemplate() {
    const sel = document.getElementById('bulkTemplateSelect');
    if (!sel) return null;
    return BULK_TEMPLATES.find(t => t.sid === sel.value) || null;
}

function onBulkTemplateChange() {
    const tpl = _getSelectedBulkTemplate();
    const container = document.getElementById('bulkVarsContainer');
    if (!tpl || !container) return;

    container.innerHTML = tpl.vars.map(v => {
        if (v.auto_fill) {
            // Variable auto-fill per-contacto — info inmutable
            return `
            <div class="bg-gray-50 border border-gray-200 rounded px-3 py-2">
                <div class="text-xs text-gray-700 font-medium">{${v.key}} ${v.label}</div>
                <div class="text-xs text-gray-500 italic mt-0.5">
                    🔄 Se usará el nombre de cada contacto automáticamente.
                </div>
            </div>`;
        }
        const existingVal = document.getElementById(`bulkVar_${v.key}`)?.value || '';
        return `
        <div>
            <label class="block text-xs font-medium text-gray-700 mb-1">
                {${v.key}} ${v.label} <span class="text-red-500">*</span>
            </label>
            <input type="text" id="bulkVar_${v.key}" required oninput="_updateBulkPreviewBox(); _scheduleBulkPreview();"
                class="w-full border rounded px-3 py-2 focus:outline-none focus:ring-2 focus:ring-[#F5C400]"
                placeholder="Ej: te escribo para preguntar si aún estás buscando"
                value="${existingVal.replace(/"/g, '&quot;')}">
        </div>`;
    }).join('');
    _updateBulkPreviewBox();
    _scheduleBulkPreview();
}

function _updateBulkPreviewBox() {
    const tpl = _getSelectedBulkTemplate();
    const box = document.getElementById('bulkPreviewBox');
    if (!tpl || !box) return;
    let rendered = tpl.preview;
    tpl.vars.forEach(v => {
        let val;
        if (v.auto_fill === 'firstname') {
            val = '[Nombre del contacto]';
        } else {
            val = document.getElementById(`bulkVar_${v.key}`)?.value.trim() || `{${v.key}}`;
        }
        rendered = rendered.split(`{${v.key}}`).join(val);
    });
    box.textContent = `"${rendered}"`;
    box.classList.remove('hidden');
}

function _collectBulkLiteralVars() {
    const tpl = _getSelectedBulkTemplate();
    if (!tpl) return null;
    const out = {};
    let hasMissing = false;
    tpl.vars.forEach(v => {
        if (v.auto_fill) return;  // se resuelve per-contacto en backend
        const val = document.getElementById(`bulkVar_${v.key}`)?.value.trim() || '';
        if (!val) hasMissing = true;
        out[v.key] = val;
    });
    return { vars: out, hasMissing, template: tpl };
}

async function _loadLastBulkCampaignBanner() {
    const banner = document.getElementById('bulkLastInfo');
    if (!banner) return;
    try {
        const url = `${BASE_URL}/bulk-campaigns/last/${encodeURIComponent(activeStageFilter)}?advisor_id=${encodeURIComponent(ADVISOR_ID)}`;
        const resp = await fetch(url, { headers: { 'X-API-Key': API_KEY } });
        const data = await resp.json();
        if (!resp.ok || !data.last) {
            banner.textContent = 'Sin envíos masivos previos en este embudo.';
            return;
        }
        const last = data.last;
        const sent = last.sent_count || 0;
        const total = last.total_contacts || 0;
        banner.innerHTML = `
            <strong>Último envío:</strong> ${_formatDateShort(last.created_at)} —
            rango ${last.date_from || '—'} → ${last.date_to || '—'} —
            ${sent}/${total} enviados (${last.status})
        `;
    } catch (e) {
        console.error('[Bulk] Error cargando banner:', e);
        banner.textContent = 'No se pudo cargar el histórico.';
    }
}

function _scheduleBulkPreview() {
    if (_bulkPreviewDebounce) clearTimeout(_bulkPreviewDebounce);
    _bulkPreviewDebounce = setTimeout(_loadBulkCampaignPreview, 500);
}

async function _loadBulkCampaignPreview() {
    const dateFrom = document.getElementById('bulkDateFrom').value || null;
    const dateTo = document.getElementById('bulkDateTo').value || null;
    const collected = _collectBulkLiteralVars();
    const previewEl = document.getElementById('bulkPreview');
    const btn = document.getElementById('bulkSendBtn');
    const countEl = document.getElementById('bulkSendCount');

    if (!collected || !collected.template) {
        previewEl.textContent = 'Selecciona una plantilla.';
        btn.disabled = true;
        countEl.textContent = '0';
        return;
    }

    previewEl.textContent = 'Calculando...';
    btn.disabled = true;
    try {
        const resp = await fetch(`${BASE_URL}/bulk-campaigns/preview`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-API-Key': API_KEY },
            body: JSON.stringify({
                stage_id: activeStageFilter,
                advisor_id: ADVISOR_ID,
                template_sid: collected.template.sid,
                template_variables: collected.vars,
                date_from: dateFrom,
                date_to: dateTo
            })
        });
        const data = await resp.json();
        if (!resp.ok) {
            previewEl.textContent = `Error: ${data.detail || 'desconocido'}`;
            return;
        }
        const total = data.total || 0;
        const totalStage = data.total_in_stage || 0;
        countEl.textContent = total;
        previewEl.innerHTML = `
            <strong>${total}</strong> contactos serán contactados
            (de ${totalStage} totales en "${data.stage_name}" asignados a ti).
            ${total !== totalStage ? '<br><span class="text-xs text-gray-600">El rango de fechas filtra por último mensaje del cliente.</span>' : ''}
        `;
        btn.disabled = total === 0 || collected.hasMissing;
    } catch (e) {
        previewEl.textContent = 'Error de red.';
        console.error('[Bulk] preview error:', e);
    }
}

async function submitBulkCampaign() {
    const dateFrom = document.getElementById('bulkDateFrom').value || null;
    const dateTo = document.getElementById('bulkDateTo').value || null;
    const collected = _collectBulkLiteralVars();
    const stageName = document.getElementById('bulkStageName').textContent;
    const total = parseInt(document.getElementById('bulkSendCount').textContent, 10) || 0;

    if (!collected || !collected.template || total === 0) return;
    if (collected.hasMissing) {
        alert('Completa todas las variables marcadas con *.');
        return;
    }

    const ok = confirm(
        `Vas a enviar a ${total} contactos TUYOS en "${stageName}".\n\n` +
        `Plantilla: ${collected.template.label}\n` +
        `Rango: ${dateFrom || 'cualquiera'} → ${dateTo || 'cualquiera'}\n\n` +
        `Esta acción no se puede deshacer. ¿Continuar?`
    );
    if (!ok) return;

    const btn = document.getElementById('bulkSendBtn');
    btn.disabled = true;
    btn.textContent = 'Creando campaña...';
    try {
        const resp = await fetch(`${BASE_URL}/bulk-campaigns`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-API-Key': API_KEY },
            body: JSON.stringify({
                stage_id: activeStageFilter,
                advisor_id: ADVISOR_ID,
                template_sid: collected.template.sid,
                template_variables: collected.vars,
                date_from: dateFrom,
                date_to: dateTo
            })
        });
        const data = await resp.json();
        if (!resp.ok) {
            alert(`Error: ${data.detail || 'desconocido'}`);
            btn.disabled = false;
            btn.textContent = `Enviar a ${total} contactos`;
            return;
        }
        closeBulkMessagingModal();
        _activeBulkCampaignId = data.campaign_id;
        sessionStorage.setItem('_activeBulkCampaign', JSON.stringify({
            id: data.campaign_id, stage_name: data.stage_name, total: data.total
        }));
        _showBulkProgressCard(data.stage_name, 0, data.total);
        _startBulkCampaignPolling(data.campaign_id);
    } catch (e) {
        alert('Error de red al crear la campaña.');
        console.error('[Bulk] submit error:', e);
        btn.disabled = false;
        btn.textContent = `Enviar a ${total} contactos`;
    }
}

function _showBulkProgressCard(stageName, sent, total) {
    const card = document.getElementById('bulkProgressCard');
    if (!card) return;
    document.getElementById('bulkProgressTitle').textContent = `Campaña: ${stageName}`;
    document.getElementById('bulkProgressText').textContent = `${sent} / ${total} enviados`;
    const pct = total > 0 ? Math.round((sent / total) * 100) : 0;
    document.getElementById('bulkProgressBar').style.width = `${pct}%`;
    card.classList.remove('hidden');
}

function hideBulkProgressCard() {
    const card = document.getElementById('bulkProgressCard');
    if (card) card.classList.add('hidden');
    if (_bulkPollTimer) {
        clearInterval(_bulkPollTimer);
        _bulkPollTimer = null;
    }
    sessionStorage.removeItem('_activeBulkCampaign');
    _activeBulkCampaignId = null;
}

function _startBulkCampaignPolling(campaignId) {
    if (_bulkPollTimer) clearInterval(_bulkPollTimer);
    _bulkPollTimer = setInterval(() => _pollBulkCampaign(campaignId), 5000);
    _pollBulkCampaign(campaignId);  // primer hit inmediato
}

async function _pollBulkCampaign(campaignId) {
    try {
        const resp = await fetch(
            `${BASE_URL}/bulk-campaigns/${campaignId}?advisor_id=${encodeURIComponent(ADVISOR_ID)}`,
            { headers: { 'X-API-Key': API_KEY } }
        );
        if (!resp.ok) {
            // 403/404 → detener polling
            if (resp.status === 403 || resp.status === 404) {
                hideBulkProgressCard();
            }
            return;
        }
        const data = await resp.json();
        const sent = data.sent_count || 0;
        const failed = data.failed_count || 0;
        const total = data.total_contacts || 0;
        _showBulkProgressCard(data.stage_name || '—', sent, total);
        if (data.status === 'completed' || data.status === 'failed') {
            clearInterval(_bulkPollTimer);
            _bulkPollTimer = null;
            const ok = sent;
            const ko = failed;
            setTimeout(() => {
                hideBulkProgressCard();
                alert(`Campaña finalizada: ${ok} enviados, ${ko} fallidos.`);
            }, 800);
        }
    } catch (e) {
        console.warn('[Bulk] polling error:', e);
    }
}

// Reanudar polling tras F5 si había campaña activa
(function _restoreActiveBulkCampaign() {
    try {
        const raw = sessionStorage.getItem('_activeBulkCampaign');
        if (!raw) return;
        const obj = JSON.parse(raw);
        if (!obj || !obj.id) return;
        _activeBulkCampaignId = obj.id;
        _showBulkProgressCard(obj.stage_name || '—', 0, obj.total || 0);
        _startBulkCampaignPolling(obj.id);
    } catch (_) {}
})();
