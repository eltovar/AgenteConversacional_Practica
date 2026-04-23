// =========================================================================
// DASHBOARD DE MÉTRICAS — Router SPA con dos vistas
// Variables API_KEY y BASE_URL inyectadas desde metrics.html (Jinja2)
// =========================================================================

let channelChart = null;
let dailyChart = null;
let appointmentsDailyChart = null;

// Cache del último payload de citas para filtrado client-side
let lastAppointmentsPayload = null;

const CHANNEL_COLORS = {
    instagram: '#E1306C',
    facebook:  '#4267B2',
    linkedin:  '#0077B5',
    youtube:   '#FF0000',
    tiktok:    '#010101',
};
const CHANNEL_COLOR_DEFAULT = '#667eea';

// =========================================================================
// ROUTER
// =========================================================================

const VIEWS = {
    social: {
        loader: loadSocialMetrics,
        exporter: exportSocialExcel,
        title: 'Dashboard Redes Sociales',
    },
    appointments: {
        loader: loadAppointmentsMetrics,
        exporter: exportAppointmentsExcel,
        title: 'Dashboard Citas Realizadas',
    },
};

let currentView = 'social';

function switchView(viewName) {
    if (!VIEWS[viewName]) return;
    currentView = viewName;
    document.querySelectorAll('[data-view]').forEach(el => {
        el.classList.toggle('hidden', el.dataset.view !== viewName);
    });
    document.getElementById('dashboardTitle').textContent = VIEWS[viewName].title;
    VIEWS[viewName].loader();
}

// =========================================================================
// VISTA: Redes Sociales (original, preservado)
// =========================================================================

function setSocialLoadingState(loading) {
    const refreshBtn = document.getElementById('refreshBtn');
    const cards = ['totalLeads', 'instagramLeads', 'facebookLeads', 'tiktokLeads'];
    if (loading) {
        refreshBtn.disabled = true;
        refreshBtn.textContent = 'Cargando...';
        cards.forEach(id => { document.getElementById(id).textContent = '…'; });
    } else {
        refreshBtn.disabled = false;
        refreshBtn.innerHTML = '&#10227; Actualizar';
    }
}

async function loadSocialMetrics() {
    const days = document.getElementById('periodSelect').value;
    console.log(`[Métricas/Social] Cargando → período: ${days} días`);
    setSocialLoadingState(true);

    try {
        const response = await fetch(`${BASE_URL}/metrics?days=${days}`, {
            headers: { 'X-API-Key': API_KEY }
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        console.log(`[Métricas/Social] total_leads: ${data.total_leads}`);
        updateSocialDashboard(data);
    } catch (error) {
        console.error('[Métricas/Social] Error:', error);
        document.getElementById('totalLeads').textContent = '—';
        document.getElementById('channelTable').innerHTML =
            '<tr><td colspan="3" class="text-center py-4 text-red-400">Error cargando datos.</td></tr>';
    } finally {
        setSocialLoadingState(false);
    }
}

async function exportSocialExcel() {
    const days = document.getElementById('periodSelect').value;
    const exportBtn = document.getElementById('exportBtn');
    exportBtn.disabled = true;
    exportBtn.textContent = 'Exportando...';
    try {
        const response = await fetch(`${BASE_URL}/metrics/export-excel?days=${days}`, {
            headers: { 'X-API-Key': API_KEY }
        });
        if (!response.ok) throw new Error('Error al exportar');
        await downloadBlobFromResponse(response, `metricas_${days}d.xlsx`);
    } catch (error) {
        console.error('Error exportando:', error);
        alert('Error al exportar: ' + error.message);
    } finally {
        exportBtn.disabled = false;
        exportBtn.innerHTML = '&#128202; Exportar Excel';
    }
}

function updateSocialDashboard(data) {
    document.getElementById('totalLeads').textContent = data.total_leads || 0;
    document.getElementById('instagramLeads').textContent = data.leads_by_channel?.instagram || 0;
    document.getElementById('facebookLeads').textContent = data.leads_by_channel?.facebook || 0;
    document.getElementById('tiktokLeads').textContent = data.leads_by_channel?.tiktok || 0;
    updateChannelChart(data.leads_by_channel || {});
    updateDailyChart(data.leads_by_day || {});
    updateChannelTable(data.leads_by_channel || {}, data.total_leads || 0);
}

function updateChannelChart(channelData) {
    const ctx = document.getElementById('channelChart').getContext('2d');
    const entries = Object.entries(channelData).filter(([, v]) => v > 0);
    const labels = entries.map(([c]) => c.charAt(0).toUpperCase() + c.slice(1));
    const values = entries.map(([, v]) => v);
    const colors = entries.map(([c]) => CHANNEL_COLORS[c.toLowerCase()] || CHANNEL_COLOR_DEFAULT);

    if (channelChart) channelChart.destroy();
    if (entries.length === 0) {
        ctx.canvas.parentElement.innerHTML = '<p class="text-center text-gray-400 py-10">Sin datos para el período</p>';
        return;
    }
    channelChart = new Chart(ctx, {
        type: 'doughnut',
        data: { labels, datasets: [{ data: values, backgroundColor: colors }] },
        options: { responsive: true, plugins: { legend: { position: 'bottom' } } }
    });
}

function updateDailyChart(dailyData) {
    const ctx = document.getElementById('dailyChart').getContext('2d');
    const labels = Object.keys(dailyData).map(d => {
        const date = new Date(d);
        return date.toLocaleDateString('es-CO', { month: 'short', day: 'numeric' });
    });
    const values = Object.values(dailyData);
    if (dailyChart) dailyChart.destroy();
    dailyChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels,
            datasets: [{
                label: 'Leads', data: values,
                borderColor: '#667eea', backgroundColor: 'rgba(102, 126, 234, 0.1)',
                fill: true, tension: 0.3,
            }]
        },
        options: {
            responsive: true,
            plugins: { legend: { display: false } },
            scales: { y: { beginAtZero: true, ticks: { stepSize: 1 } } }
        }
    });
}

function updateChannelTable(channelData, total) {
    const tbody = document.getElementById('channelTable');
    if (Object.keys(channelData).length === 0) {
        tbody.innerHTML = '<tr><td colspan="3" class="text-center py-4 text-gray-400">Sin datos</td></tr>';
        return;
    }
    tbody.innerHTML = Object.entries(channelData)
        .sort((a, b) => b[1] - a[1])
        .map(([channel, count]) => {
            const pct = total > 0 ? ((count / total) * 100).toFixed(1) : 0;
            return `
                <tr class="border-b hover:bg-gray-50">
                    <td class="py-2 capitalize">${channel}</td>
                    <td class="py-2 text-right font-semibold">${count}</td>
                    <td class="py-2 text-right text-gray-500">${pct}%</td>
                </tr>`;
        }).join('');
}

// =========================================================================
// VISTA: Citas Realizadas (NUEVA)
// =========================================================================

function getAppointmentDateRange() {
    const from = document.getElementById('dateFrom').value;
    const to = document.getElementById('dateTo').value;
    return { from, to };
}

function setAppointmentLoadingState(loading) {
    const refreshBtn = document.getElementById('refreshBtn');
    const cards = ['totalAppointments', 'weekAppointments', 'avgDayAppointments'];
    if (loading) {
        refreshBtn.disabled = true;
        refreshBtn.textContent = 'Cargando...';
        cards.forEach(id => { document.getElementById(id).textContent = '…'; });
    } else {
        refreshBtn.disabled = false;
        refreshBtn.innerHTML = '&#10227; Actualizar';
    }
}

async function loadAppointmentsMetrics() {
    const { from, to } = getAppointmentDateRange();
    if (!from || !to) {
        console.warn('[Métricas/Citas] Rango de fechas incompleto');
        return;
    }
    const workerId = document.getElementById('workerFilter').value;
    const qs = new URLSearchParams({ date_from: from, date_to: to });
    if (workerId) qs.append('worker_id', workerId);

    console.log(`[Métricas/Citas] Cargando → ${from} a ${to}, worker=${workerId || 'todos'}`);
    setAppointmentLoadingState(true);

    try {
        const response = await fetch(`${BASE_URL}/metrics/appointments?${qs.toString()}`, {
            headers: { 'X-API-Key': API_KEY }
        });
        if (!response.ok) {
            const errBody = await response.text();
            throw new Error(`HTTP ${response.status}: ${errBody}`);
        }
        const data = await response.json();
        console.log(`[Métricas/Citas] total: ${data.total}, delta: ${data.delta_pct}%`);
        lastAppointmentsPayload = data;
        renderAppointmentsDashboard(data);
    } catch (error) {
        console.error('[Métricas/Citas] Error:', error);
        document.getElementById('totalAppointments').textContent = '—';
        document.getElementById('appointmentsTable').innerHTML =
            '<tr><td colspan="5" class="text-center py-4 text-red-400">Error cargando datos.</td></tr>';
    } finally {
        setAppointmentLoadingState(false);
    }
}

function renderAppointmentsDashboard(data) {
    // Stat cards
    document.getElementById('totalAppointments').textContent = data.total ?? 0;
    document.getElementById('weekAppointments').textContent = data.week_count ?? 0;
    document.getElementById('avgDayAppointments').textContent = data.avg_per_day ?? 0;

    // Delta con color
    const deltaEl = document.getElementById('deltaPct');
    if (data.delta_pct === null || data.delta_pct === undefined) {
        deltaEl.textContent = 'Sin período previo';
        deltaEl.className = 'text-sm opacity-80 mt-2 stat-delta-neutral';
    } else {
        const sign = data.delta_pct >= 0 ? '+' : '';
        deltaEl.textContent = `${sign}${data.delta_pct}% vs. período anterior`;
        deltaEl.className = 'text-sm mt-2 ' + (
            data.delta_pct >= 0 ? 'stat-delta-positive' : 'stat-delta-negative'
        );
    }

    renderAppointmentTrendChart(data.by_day || {});
    renderAppointmentsTable(data._details || []);
}

function renderAppointmentTrendChart(byDay) {
    const ctx = document.getElementById('appointmentsDailyChart').getContext('2d');
    const labels = Object.keys(byDay).map(d => {
        const date = new Date(d + 'T00:00:00');
        return date.toLocaleDateString('es-CO', { month: 'short', day: 'numeric' });
    });
    const values = Object.values(byDay);
    if (appointmentsDailyChart) appointmentsDailyChart.destroy();
    if (labels.length === 0) {
        ctx.canvas.parentElement.innerHTML = '<h3 class="text-lg font-semibold mb-4 text-gray-700">Tendencia de Citas Realizadas</h3><p class="text-center text-gray-400 py-10">Sin citas en el período</p>';
        return;
    }
    appointmentsDailyChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels,
            datasets: [{
                label: 'Citas realizadas',
                data: values,
                borderColor: '#9333ea',
                backgroundColor: 'rgba(147, 51, 234, 0.1)',
                fill: true,
                tension: 0.3,
            }]
        },
        options: {
            responsive: true,
            plugins: { legend: { display: false } },
            scales: { y: { beginAtZero: true, ticks: { stepSize: 1 } } }
        }
    });
}

function renderAppointmentsTable(details) {
    const tbody = document.getElementById('appointmentsTable');
    const countEl = document.getElementById('appointmentsCount');
    const searchTerm = document.getElementById('searchBox').value.trim().toLowerCase();

    const filtered = searchTerm
        ? details.filter(d =>
            (d.nombre || '').toLowerCase().includes(searchTerm) ||
            (d.telefono || '').toLowerCase().includes(searchTerm)
          )
        : details;

    countEl.textContent = filtered.length === details.length
        ? `${details.length} citas`
        : `${filtered.length} de ${details.length} citas`;

    if (filtered.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" class="text-center py-4 text-gray-400">Sin citas que coincidan</td></tr>';
        return;
    }

    tbody.innerHTML = filtered.map(d => {
        const fecha = d.fecha_cita
            ? new Date(d.fecha_cita).toLocaleString('es-CO', {
                year: 'numeric', month: '2-digit', day: '2-digit',
                hour: '2-digit', minute: '2-digit'
              })
            : '—';
        const canal = (d.canal || 'whatsapp');
        return `
            <tr>
                <td>${escapeHtml(fecha)}</td>
                <td class="font-medium">${escapeHtml(d.nombre || 'Sin nombre')}</td>
                <td class="text-gray-600">${escapeHtml(d.telefono || '')}</td>
                <td>${escapeHtml(d.asesor || 'Sin asignar')}</td>
                <td class="capitalize text-gray-500">${escapeHtml(canal)}</td>
            </tr>`;
    }).join('');
}

function applyClientFilter() {
    if (!lastAppointmentsPayload) return;
    renderAppointmentsTable(lastAppointmentsPayload._details || []);
}

async function exportAppointmentsExcel() {
    const { from, to } = getAppointmentDateRange();
    if (!from || !to) {
        alert('Selecciona un rango de fechas antes de exportar');
        return;
    }
    const workerId = document.getElementById('workerFilter').value;
    const qs = new URLSearchParams({ date_from: from, date_to: to });
    if (workerId) qs.append('worker_id', workerId);

    const exportBtn = document.getElementById('exportBtn');
    exportBtn.disabled = true;
    exportBtn.textContent = 'Exportando...';
    try {
        const response = await fetch(
            `${BASE_URL}/metrics/appointments/export-excel?${qs.toString()}`,
            { headers: { 'X-API-Key': API_KEY } }
        );
        if (!response.ok) throw new Error('Error al exportar');
        await downloadBlobFromResponse(response, `citas_realizadas_${from}_a_${to}.xlsx`);
    } catch (error) {
        console.error('Error exportando citas:', error);
        alert('Error al exportar: ' + error.message);
    } finally {
        exportBtn.disabled = false;
        exportBtn.innerHTML = '&#128202; Exportar Excel';
    }
}

// Cargar trabajadores de campo (appointment_workers) una sola vez al montar.
// Construye el HTML completo en memoria antes de asignarlo al DOM para evitar
// reflows intermedios — el usuario nunca ve un dropdown parcialmente poblado.
let _workersLoaded = false;

async function loadWorkers() {
    if (_workersLoaded) return;
    const select = document.getElementById('workerFilter');
    const previousValue = select.value;
    try {
        const response = await fetch(`${BASE_URL}/workers`, {
            headers: { 'X-API-Key': API_KEY }
        });
        if (!response.ok) return;
        const data = await response.json();
        const workers = (data.workers || []).filter(w => w && w.id && w.name);

        // Construir opciones en un DocumentFragment — una sola inserción al DOM
        const frag = document.createDocumentFragment();
        const placeholder = document.createElement('option');
        placeholder.value = '';
        placeholder.textContent = 'Todos los trabajadores';
        frag.appendChild(placeholder);
        workers.forEach(w => {
            const opt = document.createElement('option');
            opt.value = w.id;
            opt.textContent = w.name;
            frag.appendChild(opt);
        });
        select.replaceChildren(frag);
        select.value = previousValue || '';
        _workersLoaded = true;
    } catch (error) {
        console.warn('[Métricas/Citas] No se pudo cargar lista de trabajadores:', error);
    }
}

// =========================================================================
// UTILITIES
// =========================================================================

async function downloadBlobFromResponse(response, fallbackName) {
    const blob = await response.blob();
    const url = window.URL.createObjectURL(blob);
    let filename = fallbackName;
    const disposition = response.headers.get('Content-Disposition');
    if (disposition) {
        const m = disposition.match(/filename\*=UTF-8''([^;]+)/i)
               || disposition.match(/filename="?([^";]+)"?/i);
        if (m) filename = decodeURIComponent(m[1].trim());
    }
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    window.URL.revokeObjectURL(url);
    a.remove();
}

function escapeHtml(s) {
    if (s === null || s === undefined) return '';
    return String(s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function debounce(fn, ms) {
    let t;
    return (...args) => {
        clearTimeout(t);
        t = setTimeout(() => fn.apply(null, args), ms);
    };
}

function initAppointmentDateDefaults() {
    const toInput = document.getElementById('dateTo');
    const fromInput = document.getElementById('dateFrom');
    const today = new Date();
    const thirtyDaysAgo = new Date(today);
    thirtyDaysAgo.setDate(today.getDate() - 30);
    const iso = d => d.toISOString().slice(0, 10);
    toInput.value = iso(today);
    fromInput.value = iso(thirtyDaysAgo);
}

// =========================================================================
// EVENT LISTENERS
// =========================================================================

document.addEventListener('DOMContentLoaded', () => {
    initAppointmentDateDefaults();
    // Cargar workers en paralelo sin bloquear la vista inicial.
    // loadWorkers() es idempotente (guardia _workersLoaded), así que llamarlo
    // antes de que el usuario abra la vista "Citas" no genera re-fetch.
    loadWorkers();
    switchView('social');  // vista inicial
});

document.getElementById('viewSelector').addEventListener('change', (e) => {
    // Al abrir la vista "appointments" por primera vez, garantizamos que
    // el dropdown ya esté poblado — si la carga inicial aún no terminó,
    // este await evita el flash "Todos los trabajadores" → lista completa.
    if (e.target.value === 'appointments') {
        loadWorkers().then(() => switchView(e.target.value));
    } else {
        switchView(e.target.value);
    }
});

document.getElementById('periodSelect').addEventListener('change', () => {
    if (currentView === 'social') loadSocialMetrics();
});

document.getElementById('dateFrom').addEventListener('change', () => {
    if (currentView === 'appointments') loadAppointmentsMetrics();
});
document.getElementById('dateTo').addEventListener('change', () => {
    if (currentView === 'appointments') loadAppointmentsMetrics();
});
document.getElementById('workerFilter').addEventListener('change', () => {
    if (currentView === 'appointments') loadAppointmentsMetrics();
});
document.getElementById('searchBox').addEventListener('input', debounce(() => {
    if (currentView === 'appointments') applyClientFilter();
}, 200));

document.getElementById('refreshBtn').addEventListener('click', () => {
    VIEWS[currentView].loader();
});

document.getElementById('exportBtn').addEventListener('click', () => {
    VIEWS[currentView].exporter();
});
