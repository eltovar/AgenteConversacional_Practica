// =========================================================================
// CONFIGURACION
// =========================================================================
// Las variables API_KEY, BASE_URL y ADVISOR_NAMES son inyectadas desde index.html

const POLLING_INTERVAL = 5000; // 5 segundos

// Leer parametro advisor de la URL
const urlParams = new URLSearchParams(window.location.search);
const ADVISOR_ID = urlParams.get('advisor');

const ADVISOR_NAME = ADVISOR_ID ? (ADVISOR_NAMES[ADVISOR_ID] || `Asesor ${ADVISOR_ID}`) : null;

let currentContactId = null;
let currentPhone = null;
let currentCanal = null;  // Canal de origen para segregacion
let pollingInterval = null;
let templatesData = [];  // Almacena templates cargados

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
        renderContactsList(data.contacts);
        updateLastUpdateTime();

        // Actualizar contador de activos
        const activeCounter = document.getElementById('activeCounter');
        if (data.active_count > 0) {
            activeCounter.innerHTML = `<span class="inline-block w-2 h-2 bg-green-500 rounded-full mr-1 animate-pulse"></span>${data.active_count} en espera`;
        } else {
            activeCounter.textContent = '';
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

        let badge = '';
        if (isInConversation) {
            badge = `${canalBadge}<span class="text-xs bg-blue-100 text-blue-700 px-2 py-0.5 rounded-full">En conversacion</span>
                     ${timeAgo ? `<p class="text-xs text-gray-400 mt-1">Llego ${timeAgo}</p>` : ''}`;
        } else if (isHumanActive || isActive) {
            badge = `${canalBadge}<span class="text-xs bg-green-100 text-green-700 px-2 py-0.5 rounded-full animate-pulse">En espera</span>
                     ${timeAgo ? `<p class="text-xs text-gray-400 mt-1">Llego ${timeAgo}</p>` : ''}
                     ${contact.ttl_display ? `<p class="text-xs text-orange-400 mt-0.5">${contact.ttl_display}</p>` : ''}`;
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

            const msgHtml = `
                <div class="flex ${isRight ? 'justify-end' : 'justify-start'} mb-3 animate-fadeIn" data-msg-id="${msg.id}">
                    <div class="${bubbleClass} p-3 shadow-sm">
                        <p class="text-xs font-semibold text-gray-600 mb-1">${msg.sender_name || msg.sender}</p>
                        <p class="text-gray-800 whitespace-pre-wrap">${escapeHtml(msg.message)}</p>
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

function updateLastUpdateTime() {
    const now = new Date().toLocaleTimeString('es-CO');
    document.getElementById('lastUpdate').textContent = `Ultima actualizacion: ${now}`;
}

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
// FUNCIONES DE INTERACCION
// =========================================================================

function selectContact(contactId, phone, displayName, canal = null) {
    currentContactId = contactId;
    currentPhone = phone;
    currentCanal = canal;  // Guardar canal para segregacion

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

    // Habilitar input
    document.getElementById('messageInput').disabled = false;
    document.getElementById('sendBtn').disabled = false;
    document.getElementById('selectedPhone').value = phone;
    document.getElementById('selectedContactId').value = contactId;

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
    event.currentTarget.classList.add('active');
}

async function sendMessage(e) {
    e.preventDefault();
    console.log('[Panel] sendMessage() iniciado');

    const phone = document.getElementById('selectedPhone').value;
    const contactId = document.getElementById('selectedContactId').value;
    const message = document.getElementById('messageInput').value.trim();
    const resultDiv = document.getElementById('sendResult');

    console.log('[Panel] Datos de envio:', { phone, contactId, messageLength: message.length });

    if (!phone || !message) {
        console.warn('[Panel] Validacion fallida: phone o message vacio');
        resultDiv.className = 'mt-2 text-sm text-red-600';
        resultDiv.textContent = 'Selecciona un contacto y escribe un mensaje';
        resultDiv.classList.remove('hidden');
        return;
    }

    // Deshabilitar mientras envia
    document.getElementById('sendBtn').disabled = true;
    document.getElementById('messageInput').disabled = true;

    try {
        const formData = new FormData();
        formData.append('to', phone);
        formData.append('body', message);
        formData.append('contact_id', contactId);
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
            resultDiv.textContent = 'Mensaje enviado correctamente';
            document.getElementById('messageInput').value = '';

            // Recargar historial (2.5s delay para que HubSpot indexe la nota)
            setTimeout(() => loadChatHistory(contactId), 2500);
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

            // Recargar historial (2.5s delay para que HubSpot indexe la nota)
            setTimeout(() => loadChatHistory(contactId), 2500);
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
// POLLING
// =========================================================================

function startPolling() {
    if (pollingInterval) clearInterval(pollingInterval);

    pollingInterval = setInterval(async () => {
        // Actualizar lista de contactos
        await loadContacts();

        // Actualizar chat si hay contacto seleccionado
        if (currentContactId) {
            await loadChatHistory(currentContactId);
        }
    }, POLLING_INTERVAL);
}

function stopPolling() {
    if (pollingInterval) {
        clearInterval(pollingInterval);
        pollingInterval = null;
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
