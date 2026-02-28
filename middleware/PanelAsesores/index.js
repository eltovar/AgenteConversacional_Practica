// =========================================================================
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

// =========================================================================
// FUNCION DE ACTUALIZACION DE ETAPA DE PIPELINE
// =========================================================================

// =========================================================================
// FUNCION DE BUSQUEDA DE CONTACTOS
// =========================================================================

function filterContacts(searchTerm) {
    const term = searchTerm.toLowerCase().trim();

    if (!term) {
        renderContactsList(allContacts);
        return;
    }

    const filtered = allContacts.filter(contact => {
        const haystack = [
            contact.display_name || '',
            contact.phone || '',
            contact.canal_origen || '',
            contact.handoff_reason || '',
        ].join(' ').toLowerCase();
        return haystack.includes(term);
    });

    renderContactsList(filtered);
}

// =========================================================================
// FUNCION DE ACTUALIZACION DE ETAPA DE PIPELINE
// =========================================================================

async function updateDealStage(contactId, dealId, stageId) {
    try {
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
    }
}

// =========================================================================
// FUNCIONES DE TEMPLATES
// =========================================================================

async function loadTemplates() {
    try {
        const response = await fetch(`${BASE_URL}/templates`, {
            headers: { 'X-API-Key': API_KEY }
        });

        if (!response.ok) throw new Error('Error al cargar templates');

        const data = await response.json();
        templatesData = data.templates || [];
        populateTemplateSelector();

    } catch (error) {
        console.error('[Panel] Error cargando templates:', error);
    }
}

function populateTemplateSelector() {
    const selector = document.getElementById('templateSelector');
    if (!selector) return;

    // Agrupar por categoria
    const categories = {};
    templatesData.forEach(t => {
        const cat = t.category || 'otros';
        if (!categories[cat]) categories[cat] = [];
        categories[cat].push(t);
    });

    // Iconos por categoria
    const categoryIcons = {
        'reactivacion': '&#128236;',
        'cita': '&#128197;',
        'seguimiento': '&#128260;',
        'recordatorio': '&#9200;',
        'promocion': '&#127919;',
        'otros': '&#128221;'
    };

    // Construir opciones agrupadas
    let html = '<option value="">-- Seleccionar template --</option>';

    Object.keys(categories).sort().forEach(cat => {
        const icon = categoryIcons[cat] || '&#128221;';
        const catName = cat.charAt(0).toUpperCase() + cat.slice(1);
        html += `<optgroup label="${icon} ${catName}">`;

        categories[cat].forEach(t => {
            html += `<option value="${t.id}">${t.name}</option>`;
        });

        html += '</optgroup>';
    });

    selector.innerHTML = html;

    // Listener para mostrar preview
    selector.onchange = showTemplatePreview;
}

function showTemplatePreview() {
    const selector = document.getElementById('templateSelector');
    const preview = document.getElementById('templatePreview');
    const sendBtn = document.getElementById('sendTemplateBtn');
    const templateId = selector.value;

    if (!templateId) {
        preview.classList.add('hidden');
        // Deshabilitar boton si no hay template seleccionado
        if (sendBtn) sendBtn.disabled = true;
        return;
    }

    const template = templatesData.find(t => t.id === templateId);
    if (template) {
        // Reemplazar variables con placeholders visuales
        let body = template.body;
        (template.variables || []).forEach(v => {
            body = body.replace(
                new RegExp(`\\{${v}\\}`, 'g'),
                `<span class="bg-yellow-200 px-1 rounded">${v}</span>`
            );
        });

        preview.innerHTML = body;
        preview.classList.remove('hidden');

        // Habilitar boton solo si hay contacto seleccionado
        if (sendBtn && currentContactId) {
            sendBtn.disabled = false;
        }
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
    formData.append('variables', JSON.stringify(variables));

    try {
        const response = await fetch(`${BASE_URL}/templates`, {
            method: 'POST',
            headers: { 'X-API-Key': API_KEY },
            body: formData
        });

        const data = await response.json();

        if (response.ok) {
            alert('Template creado exitosamente');
            await loadTemplates();
            renderTemplateList();
        } else {
            throw new Error(data.detail || 'Error creando template');
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
        const response = await fetch(`${BASE_URL}/templates/${templateId}`, {
            method: 'PUT',
            headers: { 'X-API-Key': API_KEY },
            body: formData
        });

        const data = await response.json();

        if (response.ok) {
            alert('Template actualizado');
            await loadTemplates();
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
        const response = await fetch(`${BASE_URL}/templates/${templateId}`, {
            method: 'DELETE',
            headers: { 'X-API-Key': API_KEY }
        });

        const data = await response.json();

        if (response.ok) {
            alert('Template eliminado');
            await loadTemplates();
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

async function loadContacts() {
    const filter = document.getElementById('timeFilter').value;
    let url = `${BASE_URL}/contacts?filter_time=${filter}`;

    // Agregar filtro por advisor si esta presente en la URL
    if (ADVISOR_ID) {
        url += `&advisor=${ADVISOR_ID}`;
    }

    // Agregar fechas si es filtro custom
    if (filter === 'custom') {
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
        renderContactsList(allContacts);

        // Actualizar contador: distingue "en espera" (sin responder) de "en conversacion"
        const activeCounter = document.getElementById('activeCounter');
        const inConversationCount = allContacts.filter(
            c => c.conversation_status === 'IN_CONVERSATION'
        ).length;
        const parts = [];
        if (data.active_count > 0) {
            parts.push(`<span class="inline-block w-2 h-2 bg-green-500 rounded-full mr-1 animate-pulse"></span>${data.active_count} en espera`);
        }
        if (inConversationCount > 0) {
            parts.push(`<span class="inline-block w-2 h-2 bg-blue-500 rounded-full mr-1"></span>${inConversationCount} en conversacion`);
        }
        activeCounter.innerHTML = parts.length ? parts.join(' &nbsp;·&nbsp; ') : '';

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
    console.log('[Panel] Cargando historial para contact_id:', contactId, 'canal:', currentCanal, 'phone:', currentPhone);
    try {
        // Construir URL con parametros de segregacion por canal
        let historyUrl = `${BASE_URL}/history/${contactId}?limit=50`;
        if (currentCanal) {
            historyUrl += `&canal=${encodeURIComponent(currentCanal)}`;
        }
        if (currentPhone) {
            historyUrl += `&phone=${encodeURIComponent(currentPhone)}`;
        }

        const response = await fetch(historyUrl, {
            headers: { 'X-API-Key': API_KEY }
        });

        console.log('[Panel] Respuesta de historial:', response.status);

        const data = await response.json();
        console.log('[Panel] Datos recibidos:', data, 'canal:', data.canal);

        // Verificar si hay error en la respuesta (aunque sea 200)
        if (data.error) {
            console.warn('[Panel] Error en respuesta:', data.error);
        }

        // Renderizar mensajes (puede estar vacio)
        renderChatBubbles(data.messages || []);

        // Mostrar mensaje si no hay historial
        if (!data.messages || data.messages.length === 0) {
            console.log('[Panel] Sin mensajes en historial para canal:', currentCanal);
        }

    } catch (error) {
        console.error('[Panel] Error cargando historial:', error);
        document.getElementById('chatMessages').innerHTML = `
            <div class="flex items-center justify-center h-full text-red-500">
                <p>Error al cargar historial: ${error.message}</p>
            </div>
        `;
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

// =========================================================================
// FUNCIONES DE RENDERIZADO
// =========================================================================

function renderContactsList(contacts) {
    const container = document.getElementById('contactsList');

    if (!contacts || contacts.length === 0) {
        container.innerHTML = `
            <div class="p-4 text-center text-gray-500">
                <p>No hay contactos esperando atencion</p>
                <p class="text-sm mt-1">Los contactos apareceran automaticamente cuando Sofia haga handoff</p>
            </div>
        `;
        return;
    }

    container.innerHTML = contacts.map(contact => {
        const isActive = contact.is_active === true;
        const status = contact.conversation_status || contact.status || '';
        const isInConversation = status === 'IN_CONVERSATION';
        const isHumanActive = status === 'HUMAN_ACTIVE' || status === 'PENDING_HANDOFF';
        const contactId = contact.contact_id || contact.id || '';
        const phone = contact.phone || '';
        const displayName = contact.display_name || 'Sin nombre';
        const canalOrigen = contact.canal_origen || '';  // Para segregacion por canal

        // Determinar colores segun estado
        let bgClass = '';
        let avatarClass = 'bg-gray-300';
        if (isInConversation) {
            bgClass = 'bg-blue-50 border-l-4 border-blue-500';
            avatarClass = 'bg-blue-500';
        } else if (isHumanActive || isActive) {
            bgClass = 'bg-green-50 border-l-4 border-green-500';
            avatarClass = 'bg-green-500';
        }

        // Determinar badge segun estado
        // Tiempo de llegada del contacto
        const timeAgo = contact.time_ago || '';

        // Badge del canal de origen (si existe)
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

        // Generar dropdown de pipeline si hay deal_id
        // FIX FLICKERING: Cachear deal_id para evitar que el UI oscile cuando
        // HubSpot devuelve deal_id intermitentemente
        let dealId = contact.deal_id || '';
        const cacheKey = contactId || phone;
        if (dealId && cacheKey) {
            // Si tenemos deal_id, guardarlo en cache
            contactDealCache[cacheKey] = dealId;
        } else if (cacheKey && contactDealCache[cacheKey]) {
            // Si no viene deal_id pero lo tenemos en cache, usarlo
            dealId = contactDealCache[cacheKey];
        }
        const currentStage = contact.current_stage || '';

        function buildPipelineDropdown(contactIdForDropdown, dealIdForDropdown, currentStageForDropdown) {
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

        let badge = '';
        if (isInConversation) {
            badge = `${canalBadge}<span class="text-xs bg-blue-100 text-blue-700 px-2 py-0.5 rounded-full">En conversacion</span>
                     ${timeAgo ? `<p class="text-xs text-gray-400 mt-1">Llego ${timeAgo}</p>` : ''}`;
        } else if (isHumanActive || isActive) {
            // Mostrar dropdown de pipeline si hay deal, sino mostrar badge "En espera"
            const pipelineDropdown = buildPipelineDropdown(contactId, dealId, currentStage);
            if (pipelineDropdown) {
                badge = `${canalBadge}${pipelineDropdown}
                         ${timeAgo ? `<p class="text-xs text-gray-400 mt-1">Llego ${timeAgo}</p>` : ''}
                         ${contact.ttl_display ? `<p class="text-xs text-orange-400 mt-0.5">${contact.ttl_display}</p>` : ''}`;
            } else {
                badge = `${canalBadge}<span class="text-xs bg-green-100 text-green-700 px-2 py-0.5 rounded-full animate-pulse">En espera</span>
                         ${timeAgo ? `<p class="text-xs text-gray-400 mt-1">Llego ${timeAgo}</p>` : ''}
                         ${contact.ttl_display ? `<p class="text-xs text-orange-400 mt-0.5">${contact.ttl_display}</p>` : ''}`;
            }
        } else if (status === 'BOT_ACTIVE') {
            badge = `${canalBadge}<span class="text-xs bg-gray-100 text-gray-600 px-2 py-0.5 rounded-full">Bot</span>`;
        } else {
            badge = `${canalBadge}<span class="text-xs bg-gray-100 text-gray-500 px-2 py-0.5 rounded-full">Historial</span>`;
        }

        // Generar color de fondo basado en el nombre (consistente)
        const bgColors = ['10B981', '3B82F6', 'F59E0B', 'EF4444', '8B5CF6', 'EC4899', '06B6D4'];
        const colorIndex = (displayName || 'A').charCodeAt(0) % bgColors.length;
        const bgColor = isInConversation ? '3B82F6' : (isHumanActive || isActive) ? '10B981' : bgColors[colorIndex];

        // URL de UI Avatars (servicio gratuito de avatares)
        const avatarUrl = `https://ui-avatars.com/api/?name=${encodeURIComponent(displayName || '?')}&background=${bgColor}&color=fff&size=40&rounded=true&bold=true`;

        return `
            <div class="contact-item p-3 border-b cursor-pointer ${bgClass} ${contactId === currentContactId ? 'active' : ''}"
                 onclick="selectContact('${contactId}', '${phone}', '${displayName.replace(/'/g, "\\'")}', '${canalOrigen}')">
                <div class="flex items-center gap-3">
                    <img src="${avatarUrl}"
                         class="w-10 h-10 rounded-full"
                         alt="${displayName}"
                         onerror="this.onerror=null; this.src='https://ui-avatars.com/api/?name=%3F&background=gray&color=fff&size=40&rounded=true';">
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
    }).join('');
}

// Variable para tracking de primera carga
let isFirstChatLoad = true;

function renderChatBubbles(messages) {
    const container = document.getElementById('chatMessages');

    if (!messages || messages.length === 0) {
        // Solo mostrar mensaje vacio si no hay contenido previo
        if (container.children.length === 0 || container.querySelector('[data-empty-msg]')) {
            container.innerHTML = `
                <div class="flex items-center justify-center h-full text-gray-500" data-empty-msg="true">
                    <p>No hay mensajes en el historial</p>
                </div>
            `;
        }
        return;
    }

    // SINCRONIZACION INCREMENTAL: Solo agregar mensajes nuevos
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
                ? new Date(msg.timestamp).toLocaleTimeString('es-CO', {hour: '2-digit', minute: '2-digit'})
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
            // Actualizar nombre en la UI
            const displayName = data.display_name || `${firstname} ${lastname}`.trim();
            document.getElementById('contactName').textContent = displayName;

            // Cerrar modal
            closeEditNameModal();

            // Recargar lista de contactos
            loadContacts();

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
    currentContactId = contactId;
    currentPhone = phone;
    currentCanal = canal;  // Guardar canal para segregacion
    currentName = displayName || null;

    // Reiniciar polling con intervalo mas rapido para chat activo
    restartPollingForChat();

    // Resetear estado de primera carga para nuevo contacto
    isFirstChatLoad = true;

    // Limpiar chat anterior al cambiar de contacto
    const chatContainer = document.getElementById('chatMessages');
    if (chatContainer) {
        chatContainer.innerHTML = `
            <div class="flex items-center justify-center h-full text-gray-400">
                <p>Cargando historial...</p>
            </div>
        `;
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

    // Habilitar inputs
    document.getElementById('messageInput').disabled = false;
    document.getElementById('sendBtn').disabled = false;
    document.getElementById('attachBtn').disabled = false;
    document.getElementById('recordBtn').disabled = false;  // Habilitar grabacion de audio
    document.getElementById('selectedPhone').value = phone;
    document.getElementById('selectedContactId').value = contactId;

    // Limpiar cualquier archivo multimedia previo
    clearMediaSelection();

    // =========================================================================
    // FIX v2.1: Activar HUMAN_ACTIVE al seleccionar contacto
    // Esto previene que Sofia responda mientras la asesora revisa el historial
    // =========================================================================
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
        // Continuar aunque falle - el contacto aun puede verse
    }

    // Cargar historial
    loadChatHistory(contactId);

    // Verificar ventana de 24h
    if (phone) {
        checkWindowStatus(phone);
    }

    // Actualizar lista (marcar activo)
    document.querySelectorAll('.contact-item').forEach(el => {
        el.classList.remove('active');
    });
    // Marcar el contacto actual como activo usando el contactId
    const selectedItem = document.querySelector(`.contact-item[onclick*="'${contactId}'"]`);
    if (selectedItem) {
        selectedItem.classList.add('active');
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

    // Deshabilitar boton mientras envia
    const templateBtn = document.getElementById('sendTemplateBtn');
    templateBtn.disabled = true;
    templateBtn.textContent = 'Enviando...';

    try {
        const formData = new FormData();
        formData.append('to', phone);
        formData.append('contact_id', contactId);
        formData.append('template_id', templateId);
        formData.append('variables', JSON.stringify(variables));
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
        templateBtn.disabled = false;
        templateBtn.textContent = 'Enviar Template';

        // Ocultar mensaje despues de 5 segundos
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

        // Actualizar chat si hay contacto seleccionado (polling mas frecuente)
        if (currentContactId) {
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

    // Iniciar polling
    startPolling();

    // Filtro de tiempo
    document.getElementById('timeFilter').addEventListener('change', function() {
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

    // Enviar con Ctrl+Enter
    const messageInput = document.getElementById('messageInput');
    if (messageInput) {
        messageInput.addEventListener('keydown', function(e) {
            if (e.ctrlKey && e.key === 'Enter') {
                console.log('[Panel] Ctrl+Enter presionado');
                sendMessage(e);
            }
        });
        console.log('[Panel] Event listener de Ctrl+Enter configurado');
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
const WS_RECONNECT_DELAY = 3000;  // 3 segundos

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

            // Reconectar si no fue cierre intencional
            if (wsReconnectAttempts < WS_MAX_RECONNECT_ATTEMPTS) {
                wsReconnectAttempts++;
                console.log(`[Panel] Reintentando conexion WS (${wsReconnectAttempts}/${WS_MAX_RECONNECT_ATTEMPTS})...`);
                setTimeout(connectWebSocket, WS_RECONNECT_DELAY);
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
 * Maneja mensajes recibidos del WebSocket.
 * @param {Object} data - Mensaje parseado
 */
function handleWebSocketMessage(data) {
    console.log('[Panel] Mensaje WS recibido:', data.type);

    switch (data.type) {
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

    // Refrescar lista de contactos para actualizar orden
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
}

/**
 * Maneja cambio de estado de conversacion.
 */
function handleStatusChange(data) {
    console.log('[Panel] Cambio de estado:', data.phone, data.old_status, '->', data.new_status);

    // Refrescar lista para actualizar badges
    loadContacts();
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

    // Ping cada 30 segundos para mantener conexion viva
    setInterval(sendWebSocketPing, 30000);

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

let advisorsList = [];  // Cache de asesores

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
 */
async function loadAdvisorsList() {
    const select = document.getElementById('transferToOwner');
    if (!select) return;

    // Mostrar cargando
    select.innerHTML = '<option value="">Cargando asesores...</option>';

    try {
        const response = await fetch(`${BASE_URL}/advisors`, {
            headers: { 'X-API-Key': API_KEY }
        });

        if (!response.ok) {
            throw new Error('Error cargando asesores');
        }

        const data = await response.json();
        advisorsList = data.advisors || [];

        // Limpiar y rellenar select
        select.innerHTML = '<option value="">-- Seleccionar asesora --</option>';

        for (const advisor of advisorsList) {
            const option = document.createElement('option');
            option.value = advisor.id;
            option.textContent = `${advisor.name} (${advisor.team})`;
            select.appendChild(option);
        }

        console.log('[Panel] Asesores cargados:', advisorsList.length);

    } catch (error) {
        console.error('[Panel] Error cargando asesores:', error);
        select.innerHTML = '<option value="">Error cargando asesores</option>';
    }
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
