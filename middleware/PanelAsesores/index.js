// =========================================================================
// Formatea la hora en zona Bogotá y con AM/PM claro y robusto
function formatBogotaTime(ts) {
    try {
        if (!ts) return '';
        // Forzar que el string termine en 'Z' (UTC) si no la tiene
        let safeTs = ts.trim();
        if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/.test(safeTs) && !safeTs.endsWith('Z') && !safeTs.includes('+')) {
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
const POLLING_INTERVAL_ACTIVE = 3000;  // 3 segundos cuando hay chat abierto

// Etapas del Pipeline de HubSpot
const PIPELINE_STAGES = [
    { id: "1275156339", name: "Nuevo Lead" },
    { id: "1275156340", name: "En conversacion" },
    { id: "1275156341", name: "Visita agendada" },
    { id: "1279054635", name: "Visita realizada" },
    { id: "1275312311", name: "Propuesta" },
    { id: "1279054636", name: "En estudio" },
    { id: "1275156342", name: "Cerrado ganado" },
    { id: "1279054637", name: "Cerrado vendido" }
];

// Leer parametro advisor de la URL
const urlParams = new URLSearchParams(window.location.search);
const ADVISOR_ID = urlParams.get('advisor');

const ADVISOR_NAME = ADVISOR_ID ? (ADVISOR_NAMES[ADVISOR_ID] || `Asesor ${ADVISOR_ID}`) : null;

let currentContactId = null;
let currentPhone = null;
let currentCanal = null;  // Canal de origen para segregacion
let currentName = null;   // Nombre del contacto activo para modales
let pollingInterval = null;
let templatesData = [];  // Almacena templates cargados
let allContacts = [];    // Cache de contactos para el buscador
let selectedMediaFile = null;  // Archivo multimedia seleccionado
let contactDealCache = {};  // Cache de deal_id por contacto para evitar flickering
const _contactFingerprints = new Map(); // Fingerprint del último render por phone → evita re-renders innecesarios

// Contador de mensajes no leídos por telefono (se reinicia en cada sesión)
let unreadCounts = {};
// Timestamps del último last_activity conocido por phone (para detección de nuevos mensajes en polling)
let _lastContactTimestamps = {};
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

async function filterContacts(searchTerm) {
    const term = searchTerm.toLowerCase().trim();

    if (!term) {
        renderContactsList(allContacts);
        return;
    }

    // Búsqueda local inmediata (nombre, teléfono, canal, razón)
    const localFiltered = allContacts.filter(contact => {
        const haystack = [
            contact.display_name || '',
            contact.phone || '',
            contact.canal_origen || '',
            contact.handoff_reason || '',
        ].join(' ').toLowerCase();
        return haystack.includes(term);
    });

    // Mostrar resultados locales inmediatamente
    renderContactsList(localFiltered);

    // Si el término es corto (< 3 caracteres), solo hacer búsqueda local
    if (term.length < 3) {
        return;
    }

    // Búsqueda en servidor (historial de mensajes) con debounce de 500ms
    clearTimeout(searchDebounceTimeout);
    searchDebounceTimeout = setTimeout(async () => {
        try {
            const response = await fetch(`${BASE_URL}/contacts/search?q=${encodeURIComponent(term)}&limit=20`, {
                headers: { 'X-API-Key': API_KEY }
            });

            if (!response.ok) return;

            const data = await response.json();
            const serverPhones = data.phones || [];

            if (serverPhones.length === 0) return;

            // Combinar resultados: agregar contactos del servidor que no estén en local
            const localPhones = new Set(localFiltered.map(c => c.phone));
            const additionalContacts = allContacts.filter(contact =>
                serverPhones.includes(contact.phone) && !localPhones.has(contact.phone)
            );

            if (additionalContacts.length > 0) {
                console.log(`[Panel] Búsqueda en historial: +${additionalContacts.length} contactos`);
                renderContactsList([...localFiltered, ...additionalContacts]);
            }
        } catch (error) {
            console.warn('[Panel] Error en búsqueda de historial:', error);
            // La búsqueda local ya se mostró, no hacer nada
        }
    }, 500);
}

// =========================================================================
// FUNCION DE ACTUALIZACION DE ETAPA DE PIPELINE
// =========================================================================

async function updateDealStage(contactId, dealId, stageId) {
    try {
        showLoader();
        const response = await fetch(`${BASE_URL}/contacts/${contactId}/stage`, {
            method: 'PATCH',
            headers: {
                'Content-Type': 'application/json',
                'X-API-Key': API_KEY
            },
            body: JSON.stringify({ stage_id: stageId })
        });

        const data = await response.json();

        if (response.ok) {
            // Mostrar confirmacion breve
            const stageName = PIPELINE_STAGES.find(s => s.id === stageId)?.name || stageId;
            console.log(`[Panel] Etapa actualizada: ${stageName}`);

            // ACTUALIZAR CACHÉ LOCAL para evitar flickering en próximo polling
            if (contactId && contactDealCache[contactId]) {
                contactDealCache[contactId].current_stage = stageId;
            } else if (contactId) {
                contactDealCache[contactId] = { deal_id: dealId, current_stage: stageId };
            }

            // Notificacion visual temporal
            const dropdown = document.querySelector(`select[data-contact-id="${contactId}"]`);
            if (dropdown) {
                dropdown.classList.add('ring-2', 'ring-green-400');
                setTimeout(() => {
                    dropdown.classList.remove('ring-2', 'ring-green-400');
                }, 1500);
            }
        } else {
            throw new Error(data.detail || 'Error actualizando etapa');
        }
    } catch (error) {
        console.error('[Panel] Error actualizando etapa:', error);
        alert('Error al actualizar etapa: ' + error.message);
        // Recargar contactos para revertir el dropdown
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
    // Escapar caracteres especiales de regex en el template original
    let pattern = originalBody.replace(/[-[\]/{}()*+?.\\^$|]/g, '\\$&');
    // Reemplazar cada \{varname\} con un grupo de captura
    variables.forEach(v => {
        pattern = pattern.replace(`\\{${v}\\}`, '([\\s\\S]+?)');
    });
    // El último grupo debe ser greedy para capturar hasta el final
    pattern = pattern.replace(/\(\[\\s\\S\]\+\?\)(?=[^(]*$)/, '([\\s\\S]+)');
    try {
        const match = editedText.match(new RegExp('^' + pattern + '$'));
        if (!match) return {};
        const result = {};
        variables.forEach((v, i) => { result[v] = match[i + 1] || ''; });
        return result;
    } catch (e) {
        // Si el regex falla, devolver objeto vacío (backend usa SafeDict)
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
                    <button onclick="showCreateTemplateForm()" class="bg-green-500 text-white px-4 py-2 rounded hover:bg-green-600">
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
                <button type="submit" class="bg-green-500 text-white px-4 py-2 rounded hover:bg-green-600">
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
    const timeFilter = document.getElementById('timeFilter');
    if (timeFilter) timeFilter.disabled = !!workerId;
    loadContacts();
}

async function loadContacts() {
    const workerSel = document.getElementById('workerFilter');
    const workerIdParam = workerSel ? workerSel.value : '';

    // Si hay filtro worker activo, ignorar filtro de tiempo
    const filter = document.getElementById('timeFilter').value;
    let url = `${BASE_URL}/contacts?filter_time=${filter}`;

    if (workerIdParam) {
        url = `${BASE_URL}/contacts?worker_id=${encodeURIComponent(workerIdParam)}`;
    }

    // Agregar filtro por advisor si esta presente en la URL
    if (ADVISOR_ID) {
        url += `&advisor=${ADVISOR_ID}`;
    }

    // Agregar fechas si es filtro custom (solo cuando no hay worker filter)
    if (!workerIdParam && filter === 'custom') {
        const dateFrom = document.getElementById('dateFrom').value;
        const dateTo = document.getElementById('dateTo').value;
        if (dateFrom) url += `&date_from=${dateFrom}T00:00:00`;
        if (dateTo) url += `&date_to=${dateTo}T23:59:59`;
    }

    try {
        const response = await fetch(url, {
            headers: { 'X-API-Key': API_KEY }
        });

        if (!response.ok) throw new Error('Error al cargar contactos');

        const data = await response.json();
        allContacts = data.contacts || [];  // Guardar en cache para el buscador

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
                        unreadCounts[phone] = (unreadCounts[phone] || 0) + 1;
                        updateUnreadBadge(phone, unreadCounts[phone]);
                    }
                    playNotificationSound();
                }
            }
        }

        renderContactsList(allContacts);

        // Auto-select por deep link (?phone=): ejecutar solo una vez tras la primera carga
        if (!deepLinkHandled) {
            let deepLinkPhone = urlParams.get('phone');
            if (deepLinkPhone) {
                deepLinkHandled = true;
                // Normalizar: quitar espacios y agregar + si no lo tiene
                // (URLs convierten + en espacio, así que "+" llega como " ")
                deepLinkPhone = deepLinkPhone.replace(/\s+/g, '').trim();
                if (!deepLinkPhone.startsWith('+')) {
                    deepLinkPhone = '+' + deepLinkPhone;
                }
                const target = allContacts.find(c => c.phone === deepLinkPhone);
                if (target) {
                    console.log('[Panel] Deep link: auto-seleccionando', deepLinkPhone);
                    selectContact(target.contact_id || '', target.phone, target.display_name || target.phone, target.canal_origen || 'whatsapp');
                } else {
                    console.warn('[Panel] Deep link: contacto no encontrado en lista activa:', deepLinkPhone);
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

async function loadChatHistory(contactId) {
    // Capturar el contactId y phone al momento de iniciar la petición
    // para verificar que no cambió durante el fetch (race condition fix)
    const requestedContactId = contactId;
    const requestedPhone = currentPhone;
    const requestedCanal = currentCanal;

    console.log('[Panel] Cargando historial para contact_id:', requestedContactId, 'canal:', requestedCanal, 'phone:', requestedPhone);
    try {
        // Construir URL con parametros de segregacion por canal
        let historyUrl = `${BASE_URL}/history/${requestedContactId}?limit=50`;
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

        // Renderizar mensajes (puede estar vacio)
        renderChatBubbles(data.messages || []);

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

    try {
        let url = `${BASE_URL}/contacts/${encodeURIComponent(phone)}/detail?limit=50`;
        if (contactId) url += `&contact_id=${encodeURIComponent(contactId)}`;
        if (canal) url += `&canal=${encodeURIComponent(canal)}`;

        const response = await fetch(url, { headers: { 'X-API-Key': API_KEY } });

        // Race condition: si el contacto cambió mientras esperábamos, descartar
        if (currentContactId !== requestedContactId) {
            console.warn(`[Panel] Race condition en loadContactDetail: descartado (${requestedContactId})`);
            return;
        }

        const data = await response.json();

        // Renderizar mensajes
        renderChatBubbles(data.messages || []);

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
function _buildPipelineDropdown(contactIdForDropdown, dealIdForDropdown, currentStageForDropdown) {
    if (!dealIdForDropdown) return '';
    const options = PIPELINE_STAGES.map(stage =>
        `<option value="${stage.id}" ${stage.id === currentStageForDropdown ? 'selected' : ''}>${stage.name}</option>`
    ).join('');
    return `
        <select class="text-xs border rounded px-1 py-0.5 bg-white cursor-pointer hover:border-blue-400 focus:ring-1 focus:ring-blue-400"
                data-contact-id="${contactIdForDropdown}"
                onchange="updateDealStage('${contactIdForDropdown}', '${dealIdForDropdown}', this.value)"
                onclick="event.stopPropagation()">
            ${options}
        </select>
    `;
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
    const dealId = contact.deal_id || (cached ? (cached.deal_id || cached) : '');
    const currentStage = contact.current_stage || (cached?.current_stage) || '';
    return [
        contact.conversation_status || contact.status || '',
        contact.is_active ? '1' : '0',
        contact.display_name || '',
        contact.canal_origen || '',
        dealId,
        currentStage,
        contact.time_ago || '',
        contact.ttl_display || '',
        contact.handoff_reason || '',
        contact.has_appointment ? '1' : '0',
        contactId === currentContactId ? 'active' : '',
    ].join('|');
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
    if (isInConversation) {
        bgClass = 'bg-blue-50 border-l-4 border-blue-500';
    } else if (isHumanActive || isActive) {
        bgClass = 'bg-green-50 border-l-4 border-green-500';
    }

    const timeAgo = contact.time_ago || '';

    const canalColors = {
        'instagram': 'bg-pink-100 text-pink-700',
        'facebook': 'bg-blue-100 text-blue-700',
        'finca_raiz': 'bg-yellow-100 text-yellow-700',
        'metrocuadrado': 'bg-orange-100 text-orange-700',
        'pagina_web': 'bg-indigo-100 text-indigo-700',
        'whatsapp_directo': 'bg-green-100 text-green-700',
        'default': 'bg-gray-100 text-gray-600'
    };
    const canalColorClass = canalColors[canalOrigen] || canalColors['default'];
    const canalBadge = canalOrigen && canalOrigen !== 'default'
        ? `<span class="text-xs ${canalColorClass} px-1.5 py-0.5 rounded mr-1">${canalOrigen.replace('_', ' ')}</span>`
        : '';

    let dealId = contact.deal_id || '';
    let currentStage = contact.current_stage || '';
    const cacheKey = contactId || phone;

    if (dealId && cacheKey) {
        contactDealCache[cacheKey] = {
            deal_id: dealId,
            current_stage: currentStage || contactDealCache[cacheKey]?.current_stage || ''
        };
    } else if (cacheKey && contactDealCache[cacheKey]) {
        dealId = contactDealCache[cacheKey].deal_id || contactDealCache[cacheKey];
        if (!currentStage && contactDealCache[cacheKey].current_stage) {
            currentStage = contactDealCache[cacheKey].current_stage;
        }
    }

    let badge = '';
    if (isInConversation || isHumanActive || isActive) {
        const pipelineDropdown = _buildPipelineDropdown(contactId, dealId, currentStage);
        if (pipelineDropdown) {
            badge = `${canalBadge}${pipelineDropdown}
                     ${timeAgo ? `<p class="text-xs text-gray-400 mt-1">Llego ${timeAgo}</p>` : ''}
                     ${contact.ttl_display ? `<p class="text-xs text-orange-400 mt-0.5">${contact.ttl_display}</p>` : ''}`;
        } else if (isInConversation) {
            badge = `${canalBadge}<span class="text-xs bg-blue-100 text-blue-700 px-2 py-0.5 rounded-full">En conversacion</span>
                     ${timeAgo ? `<p class="text-xs text-gray-400 mt-1">Llego ${timeAgo}</p>` : ''}`;
        } else {
            badge = `${canalBadge}<span class="text-xs bg-green-100 text-green-700 px-2 py-0.5 rounded-full animate-pulse">En espera</span>
                     ${timeAgo ? `<p class="text-xs text-gray-400 mt-1">Llego ${timeAgo}</p>` : ''}
                     ${contact.ttl_display ? `<p class="text-xs text-orange-400 mt-0.5">${contact.ttl_display}</p>` : ''}`;
        }
    } else if (status === 'BOT_ACTIVE') {
        badge = `${canalBadge}`;
    } else {
        badge = `${canalBadge}<span class="text-xs bg-gray-100 text-gray-500 px-2 py-0.5 rounded-full">Historial</span>`;
    }

    const bgColors = ['10B981', '3B82F6', 'F59E0B', 'EF4444', '8B5CF6', 'EC4899', '06B6D4'];
    const colorIndex = (displayName || 'A').charCodeAt(0) % bgColors.length;
    const bgColor = isInConversation ? '3B82F6' : (isHumanActive || isActive) ? '10B981' : bgColors[colorIndex];
    const avatarUrl = `https://ui-avatars.com/api/?name=${encodeURIComponent(displayName || '?')}&background=${bgColor}&color=fff&size=40&rounded=true&bold=true`;

    const unread = unreadCounts[phone] || 0;
    const unreadBadge = (unread > 0 && phone !== currentPhone)
        ? `<span class="unread-badge absolute -top-1 -right-1 bg-red-500 text-white text-xs rounded-full min-w-[18px] h-[18px] flex items-center justify-center font-bold px-0.5 leading-none">${unread > 9 ? '9+' : unread}</span>`
        : '';

    const apptBadge = contact.has_appointment
        ? `<span class="absolute -bottom-1 -right-1 bg-amber-400 text-white text-xs rounded-full w-[18px] h-[18px] flex items-center justify-center leading-none" title="Tiene cita programada">📅</span>`
        : '';

    const stateBadge = (isInConversation || isHumanActive || isActive)
        ? `<span class="absolute -top-1 -left-1 bg-blue-100 rounded-full w-[18px] h-[18px] flex items-center justify-center text-[10px] leading-none" title="Asesora atendiendo">👤</span>`
        : status === 'BOT_ACTIVE'
        ? `<span class="absolute -top-1 -left-1 bg-gray-100 rounded-full w-[18px] h-[18px] flex items-center justify-center text-[10px] leading-none" title="Sofía está manejando">🤖</span>`
        : '';

    return `
        <div class="contact-item p-3 border-b cursor-pointer ${bgClass} ${contactId === currentContactId ? 'active' : ''}"
             data-phone="${phone}"
             onclick="selectContact('${contactId}', '${phone}', '${displayName.replace(/'/g, "\\'")}', '${canalOrigen}')">
            <div class="flex items-center gap-3">
                <div class="relative flex-shrink-0">
                    <img src="${avatarUrl}"
                         class="w-10 h-10 rounded-full"
                         alt="${displayName}"
                         onerror="this.onerror=null; this.src='https://ui-avatars.com/api/?name=%3F&background=gray&color=fff&size=40&rounded=true';">
                    ${stateBadge}
                    ${unreadBadge}
                    ${apptBadge}
                </div>
                <div class="flex-1 min-w-0">
                    <p class="font-medium text-gray-800 truncate">${displayName}</p>
                    <p class="text-sm text-gray-500 truncate">${phone || contact.email || 'Sin contacto'}</p>
                    ${contact.handoff_reason ? `<p class="text-xs text-gray-400 truncate">${contact.handoff_reason}</p>` : ''}
                </div>
                <div class="text-right">
                    ${badge}
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
    const container = document.getElementById('contactsList');

    if (!contacts || contacts.length === 0) {
        container.innerHTML = `
            <div class="p-4 text-center text-gray-400">
                <p>No hay contactos esperando atención.</p>
            </div>
        `;
        _contactFingerprints.clear();
        return;
    }

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
    }
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

    messages.forEach(msg => {
        // Verificar si el mensaje ya existe en el DOM usando data-msg-id
        const existingMsg = container.querySelector(`[data-msg-id="${msg.id}"]`);

        if (!existingMsg) {
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
                    // Detectar tipo de audio por extension para mejor compatibilidad
                    let audioType = 'audio/mpeg';
                    if (mediaUrl.includes('.webm')) audioType = 'audio/webm';
                    else if (mediaUrl.includes('.ogg')) audioType = 'audio/ogg';
                    else if (mediaUrl.includes('.mp4') || mediaUrl.includes('.m4a')) audioType = 'audio/mp4';

                    mediaHtml = `
                        <div class="mb-2">
                            <audio controls class="w-full" style="max-width: 280px;">
                                <source src="${mediaUrl}" type="${audioType}">
                                Tu navegador no soporta audio.
                            </audio>
                        </div>`;
                    // Mostrar transcripcion de audio si existe
                    if (transcription) {
                        mediaHtml += `
                            <div class="mt-1 p-2 bg-green-50 rounded text-xs text-green-700">
                                <span class="font-medium">Transcripcion:</span> ${escapeHtml(transcription)}
                            </div>`;
                    }
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

            const msgHtml = `
                <div class="flex ${isRight ? 'justify-end' : 'justify-start'} mb-3 animate-fadeIn" data-msg-id="${msg.id}">
                    <div class="${bubbleClass} p-3 shadow-sm">
                        <p class="text-xs font-semibold text-gray-600 mb-1">${msg.sender_name || msg.sender}</p>
                        ${mediaHtml}
                        ${msg.message ? `<p class="text-gray-800 whitespace-pre-wrap">${escapeHtml(msg.message)}</p>` : ''}
                        <p class="text-xs text-gray-500 text-right mt-1">${timestamp}</p>
                    </div>
                </div>
            `;

            container.insertAdjacentHTML('beforeend', msgHtml);
            hasNewContent = true;
        }
    });

    // Solo hacer scroll si hay contenido nuevo o es la primera carga
    if (hasNewContent || isFirstChatLoad) {
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
                            class="w-full border rounded px-3 py-2 focus:outline-none focus:ring-2 focus:ring-green-500"
                            placeholder="Nombre">
                    </div>
                    <div class="mb-4">
                        <label class="block text-sm font-medium mb-1">Apellido</label>
                        <input type="text" id="editLastname" name="lastname"
                            class="w-full border rounded px-3 py-2 focus:outline-none focus:ring-2 focus:ring-green-500"
                            placeholder="Apellido (opcional)">
                    </div>
                    <div class="flex gap-2 justify-end">
                        <button type="button" onclick="closeEditNameModal()" class="px-4 py-2 bg-gray-200 rounded hover:bg-gray-300">
                            Cancelar
                        </button>
                        <button type="submit" class="px-4 py-2 bg-green-500 text-white rounded hover:bg-green-600">
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
            // Limpiar seleccion actual
            currentContactId = null;
            currentPhone = null;
            currentCanal = null;
            currentName = null;

            // Resetear UI
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

            // Recargar lista de contactos
            await loadContacts();

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
                renderContactsList(allContacts);
            }
            closeEditNameModal();
            loadContacts();  // sync adicional con backend en background
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
    const validTypes = ['image/jpeg', 'image/png', 'image/gif', 'image/webp', 'audio/mpeg', 'audio/wav', 'audio/ogg', 'audio/webm'];
    if (!validTypes.includes(file.type)) {
        alert('Tipo de archivo no soportado. Solo imagenes y audios.');
        input.value = '';
        return;
    }

    // Validar tamano (max 16MB para Bunny.net CDN)
    const maxSize = 16 * 1024 * 1024;
    if (file.size > maxSize) {
        alert('El archivo es demasiado grande. Maximo 16MB.');
        input.value = '';
        return;
    }

    selectedMediaFile = file;

    // Mostrar preview
    const preview = document.getElementById('mediaPreview');
    const previewName = document.getElementById('mediaPreviewName');

    const icon = file.type.startsWith('image/') ? '&#128247;' : '&#127911;';
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
// FUNCIONES DE INTERACCION
// =========================================================================

async function selectContact(contactId, phone, displayName, canal = null) {
    // Si es el mismo contacto, no hacer nada (evita recargas innecesarias)
    if (currentContactId === contactId && currentPhone === phone) {
        console.log('[Panel] Mismo contacto seleccionado, ignorando');
        return;
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

    // Mostrar botón de agendar cita solo cuando hay un contacto activo
    const apptBtn = document.getElementById('scheduleAppointmentBtn');
    if (apptBtn) {
        apptBtn.classList.toggle('hidden', !contactId);
    }

    currentContactId = contactId;
    currentPhone = phone;
    currentCanal = canal;  // Guardar canal para segregacion
    currentName = displayName || null;

    // Limpiar estado del template picker al cambiar de contacto
    activeTemplateId = null; activeTemplateBody = ''; activeTemplateVars = [];
    closeTemplatePicker();
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
    document.querySelectorAll('.contact-item').forEach(el => {
        el.classList.remove('active');
    });
    const selectedItem = document.querySelector(`.contact-item[onclick*="'${contactId}'"]`);
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
        const vars = extractVariableValues(activeTemplateBody, message, activeTemplateVars) || {};
        const tplId = activeTemplateId;
        // Limpiar estado del template antes de enviar
        activeTemplateId = null; activeTemplateBody = ''; activeTemplateVars = [];
        const hint = document.getElementById('templateHint');
        if (hint) hint.classList.add('hidden');
        await sendTemplateFromInput(tplId, vars);
        return;
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
                const mediaLabel = data.media_type === 'image' ? 'imagen' : (data.media_type === 'audio' ? 'audio' : 'archivo');
                resultDiv.textContent = `Mensaje con ${mediaLabel} enviado correctamente`;
            } else {
                resultDiv.textContent = 'Mensaje enviado correctamente';
            }

            document.getElementById('messageInput').value = '';

            // Limpiar seleccion de archivo
            clearMediaSelection();

            // ARQUITECTURA v2.0: MongoDB es la fuente de verdad en tiempo real
            // El mensaje ya esta disponible en MongoDB (~5ms), no necesitamos
            // polling incremental complejo. Un refresh inmediato es suficiente.
            loadChatHistory(contactId);
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
        resultDiv.textContent = `Error: ${err.message}`;
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
    pollingInterval = setInterval(async () => {
        if (!isTabVisible) return;
        await loadContacts();
    }, POLLING_INTERVAL_IDLE);
    console.log('[Panel] Fallback polling activo (WS conectado): 10s');
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

    // Inicializar listeners del template picker (slash command)
    _initTemplatePickerListeners();

    // Iniciar polling
    startPolling();

    // Filtro de tiempo
    document.getElementById('timeFilter').addEventListener('change', function () {
        const customDates = document.getElementById('customDates');
        if (this.value === 'custom') {
            customDates.classList.remove('hidden');
        } else {
            customDates.classList.add('hidden');
            loadContacts();
        }
    });

    // Boton refresh
    document.getElementById('refreshBtn').addEventListener('click', loadContacts);

    // Aplicar fechas custom
    document.getElementById('applyDatesBtn').addEventListener('click', loadContacts);

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

            // Reconectar con backoff exponencial
            if (wsReconnectAttempts < WS_MAX_RECONNECT_ATTEMPTS) {
                wsReconnectAttempts++;
                wsCurrentDelay = Math.min(WS_BASE_RECONNECT_DELAY * Math.pow(2, wsReconnectAttempts - 1), 30000);
                console.log(`[Panel] Reintentando conexion WS (${wsReconnectAttempts}/${WS_MAX_RECONNECT_ATTEMPTS}) en ${wsCurrentDelay}ms...`);
                setTimeout(connectWebSocket, wsCurrentDelay);
            } else {
                console.warn('[Panel] Max reintentos WS alcanzado. Usando solo polling.');
            }
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
    if (!contactEl) return;
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
    } else if (badge) {
        badge.remove();
    }
}

/**
 * Programa un refresco de la lista de contactos con debounce de 500ms.
 * Agrupa múltiples eventos WS rápidos en una sola llamada a loadContacts().
 */
let _contactsRefreshTimer = null;
function scheduleContactsRefresh() {
    if (_contactsRefreshTimer) return;  // Ya hay uno pendiente, no acumular
    _contactsRefreshTimer = setTimeout(async () => {
        _contactsRefreshTimer = null;
        await loadContacts();
    }, 500);
}

/**
 * Maneja mensajes recibidos del WebSocket.
 * @param {Object} data - Mensaje parseado
 */
function handleWebSocketMessage(data) {
    console.log('[Panel] Mensaje WS recibido:', data.type, data);

    switch (data.type) {
        case 'contact_updated':
            // Nuevo mensaje o actividad → reordenar lista INMEDIATAMENTE
            console.log('[Panel] contact_updated recibido, action:', data.action, 'phone:', data.phone);

            // Si action es 'new_message', actualizar badge directamente sin re-render
            if (data.action === 'new_message' && data.phone && data.phone !== currentPhone) {
                console.log('[Panel] Incrementando unreadCounts para', data.phone);
                unreadCounts[data.phone] = (unreadCounts[data.phone] || 0) + 1;
                updateUnreadBadge(data.phone, unreadCounts[data.phone]);  // DOM directo, sin re-render
                playNotificationSound();
                if (document.hidden) {
                    showBrowserNotification(data.phone, 'Nuevo mensaje');
                }
            }

            // Refresco debounced: agrupa eventos rápidos en una sola carga
            scheduleContactsRefresh();
            // Si el chat activo es el que recibió el mensaje, refrescar historial
            if (currentPhone && data.phone && data.phone === currentPhone) {
                loadChatHistory(currentContactId);
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

/**
 * Maneja notificacion de nuevo mensaje.
 */
function handleNewMessageNotification(data) {
    console.log('[Panel] Nuevo mensaje de', data.phone, ':', data.preview);

    // Reproducir sonido de notificacion
    playNotificationSound();

    // Mostrar notificacion del navegador si la pestana esta oculta
    if (document.hidden) {
        showBrowserNotification(
            data.contact_name || data.phone,
            data.preview || 'Nuevo mensaje'
        );
    }

    // Tracking de mensajes no leídos (solo si el chat de ese contacto NO está abierto)
    if (data.phone && data.phone !== currentPhone) {
        unreadCounts[data.phone] = (unreadCounts[data.phone] || 0) + 1;
    }

    // Refrescar lista de contactos para actualizar orden (y badge de no leídos)
    loadContacts();

    // Si el contacto actual es el que envio mensaje, refrescar chat
    if (currentPhone === data.phone) {
        loadChatHistory(currentContactId);
    }
}

/**
 * Maneja notificacion de transferencia de contacto.
 */
function handleContactTransferred(data) {
    console.log('[Panel] Contacto transferido:', data);

    // Notificacion visual
    if (data.direction === 'incoming') {
        playNotificationSound();
        showBrowserNotification(
            'Nuevo contacto',
            `${data.contact_name || data.phone} ha sido transferido a tu panel`
        );
    }

    // Refrescar lista
    loadContacts();
    // Actualizar badge de no leídos instantáneamente si aplica
    if (data.phone) {
        unreadCounts[data.phone] = 0;
        updateUnreadBadge(data.phone, 0);
    }
}

/**
 * Maneja cambio de estado de conversacion.
 */
function handleStatusChange(data) {
    console.log('[Panel] Cambio de estado:', data.phone, data.old_status, '->', data.new_status);

    // Refrescar lista para actualizar badges
    loadContacts();
    // Resetear badge de no leídos si el estado es "cerrado" o similar
    if (data.phone && ['cerrado', 'cerrado ganado', 'cerrado vendido'].includes((data.new_status || '').toLowerCase())) {
        unreadCounts[data.phone] = 0;
        updateUnreadBadge(data.phone, 0);
    }
}

/**
 * Reproduce sonido de notificacion.
 */
function playNotificationSound() {
    try {
        const audio = document.getElementById('notificationSound');
        if (audio) {
            audio.currentTime = 0;
            audio.volume = 0.5;
            audio.play().catch(e => {
                // El navegador puede bloquear autoplay hasta interaccion del usuario
                console.log('[Panel] Audio bloqueado por navegador');
            });
        }
    } catch (e) {
        console.warn('[Panel] Error reproduciendo audio:', e);
    }
}

/**
 * Muestra notificacion del navegador.
 */
function showBrowserNotification(title, body) {
    // Verificar si las notificaciones estan soportadas y permitidas
    if (!('Notification' in window)) {
        return;
    }

    if (Notification.permission === 'granted') {
        new Notification(title, {
            body: body,
            icon: 'https://ui-avatars.com/api/?name=P&background=10B981&color=fff&size=64',
            tag: 'panel-notification'  // Evita multiples notificaciones
        });
    } else if (Notification.permission !== 'denied') {
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
        // Limpiar formulario
        const form = document.getElementById('createContactForm');
        if (form) form.reset();
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
            // Contacto ya existe
            showCreateResult('warning',
                `${data.message}. <button onclick="takeControlOfExisting('${data.contact_id}', '${data.phone}')" class="underline font-medium">Tomar control</button>`
            );
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
                    selectContact(data.contact_id, data.phone, data.display_name, 'whatsapp_directo');
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

/**
 * Toma control de un contacto existente (cuando se detecta duplicado).
 * @param {string} contactId - ID del contacto en HubSpot
 * @param {string} phone - Telefono normalizado
 */
async function takeControlOfExisting(contactId, phone) {
    console.log('[Panel] Tomando control de contacto existente:', contactId, phone);

    try {
        // Usar el endpoint de take-control existente
        const takeControlUrl = `${BASE_URL}/contacts/${encodeURIComponent(phone)}/take-control?` +
            `canal=whatsapp_directo&contact_id=${encodeURIComponent(contactId)}`;

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
                class="hidden flex-1 text-sm border rounded px-2 py-1 focus:ring-1 focus:ring-green-400 worker-name-input">
            <button onclick="startEditWorker('${w.id}')"
                class="text-blue-500 hover:text-blue-700 text-xs px-2 py-1 worker-edit-btn"
                title="Editar nombre">&#9998;</button>
            <button onclick="saveEditWorker('${w.id}')"
                class="hidden text-green-600 hover:text-green-800 text-xs px-2 py-1 worker-save-btn"
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
        document.getElementById('apptDatetime').value = dt.toISOString().slice(0, 16);
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

            // Recargar y volver a lista
            setTimeout(async () => {
                await _loadAppointmentsAndRender();
                loadContacts(); // Actualizar badge
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
