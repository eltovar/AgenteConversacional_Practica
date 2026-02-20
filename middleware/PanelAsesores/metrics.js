// =========================================================================
// METRICAS DASHBOARD
// Variables API_KEY y BASE_URL son inyectadas desde metrics.html
// =========================================================================

let channelChart = null;
let dailyChart = null;

async function loadMetrics() {
    const days = document.getElementById('periodSelect').value;

    try {
        const response = await fetch(`${BASE_URL}/metrics?days=${days}`, {
            headers: { 'X-API-Key': API_KEY }
        });

        if (!response.ok) throw new Error('Error al cargar metricas');

        const data = await response.json();
        updateDashboard(data);

    } catch (error) {
        console.error('Error:', error);
        document.getElementById('totalLeads').textContent = 'Error';
        alert('Error cargando metricas: ' + error.message);
    }
}

async function exportCSV() {
    const days = document.getElementById('periodSelect').value;
    const exportBtn = document.getElementById('exportBtn');

    exportBtn.disabled = true;
    exportBtn.textContent = 'Exportando...';

    try {
        const response = await fetch(`${BASE_URL}/metrics/export?days=${days}`, {
            headers: { 'X-API-Key': API_KEY }
        });

        if (!response.ok) throw new Error('Error al exportar');

        // Obtener el blob y crear descarga
        const blob = await response.blob();
        const url = window.URL.createObjectURL(blob);

        // Obtener nombre del archivo del header
        const disposition = response.headers.get('Content-Disposition');
        let filename = `metricas_${days}d.csv`;
        if (disposition) {
            const match = disposition.match(/filename=([^;]+)/);
            if (match) filename = match[1];
        }

        // Crear link de descarga y hacer click
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        window.URL.revokeObjectURL(url);
        a.remove();

    } catch (error) {
        console.error('Error exportando:', error);
        alert('Error al exportar: ' + error.message);
    } finally {
        exportBtn.disabled = false;
        exportBtn.textContent = '&#128202; Exportar CSV';
    }
}

function updateDashboard(data) {
    // Update stat cards
    document.getElementById('totalLeads').textContent = data.total_leads || 0;
    document.getElementById('instagramLeads').textContent = data.leads_by_channel?.instagram || 0;
    document.getElementById('facebookLeads').textContent = data.leads_by_channel?.facebook || 0;
    document.getElementById('tiktokLeads').textContent = data.leads_by_channel?.tiktok || 0;

    // Update channel chart
    updateChannelChart(data.leads_by_channel || {});

    // Update daily chart
    updateDailyChart(data.leads_by_day || {});

    // Update table
    updateChannelTable(data.leads_by_channel || {}, data.total_leads || 0);
}

function updateChannelChart(channelData) {
    const ctx = document.getElementById('channelChart').getContext('2d');

    const labels = Object.keys(channelData).map(c => c.charAt(0).toUpperCase() + c.slice(1));
    const values = Object.values(channelData);

    if (channelChart) channelChart.destroy();

    channelChart = new Chart(ctx, {
        type: 'doughnut',
        data: {
            labels: labels,
            datasets: [{
                data: values,
                backgroundColor: [
                    '#E1306C', // Instagram
                    '#4267B2', // Facebook
                    '#0077B5', // LinkedIn
                    '#FF0000', // YouTube
                    '#000000', // TikTok
                ],
            }]
        },
        options: {
            responsive: true,
            plugins: {
                legend: {
                    position: 'bottom',
                }
            }
        }
    });
}

function updateDailyChart(dailyData) {
    const ctx = document.getElementById('dailyChart').getContext('2d');

    const labels = Object.keys(dailyData).map(d => {
        const date = new Date(d);
        return date.toLocaleDateString('es-CO', {month: 'short', day: 'numeric'});
    });
    const values = Object.values(dailyData);

    if (dailyChart) dailyChart.destroy();

    dailyChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [{
                label: 'Leads',
                data: values,
                borderColor: '#667eea',
                backgroundColor: 'rgba(102, 126, 234, 0.1)',
                fill: true,
                tension: 0.3,
            }]
        },
        options: {
            responsive: true,
            plugins: {
                legend: {
                    display: false
                }
            },
            scales: {
                y: {
                    beginAtZero: true,
                    ticks: {
                        stepSize: 1
                    }
                }
            }
        }
    });
}

function updateChannelTable(channelData, total) {
    const tbody = document.getElementById('channelTable');

    if (Object.keys(channelData).length === 0) {
        tbody.innerHTML = '<tr><td colspan="3" class="text-center py-4 text-gray-400">Sin datos</td></tr>';
        return;
    }

    const rows = Object.entries(channelData)
        .sort((a, b) => b[1] - a[1])
        .map(([channel, count]) => {
            const pct = total > 0 ? ((count / total) * 100).toFixed(1) : 0;
            return `
                <tr class="border-b hover:bg-gray-50">
                    <td class="py-2 capitalize">${channel}</td>
                    <td class="py-2 text-right font-semibold">${count}</td>
                    <td class="py-2 text-right text-gray-500">${pct}%</td>
                </tr>
            `;
        })
        .join('');

    tbody.innerHTML = rows;
}

// Event listeners
document.addEventListener('DOMContentLoaded', loadMetrics);
document.getElementById('periodSelect').addEventListener('change', loadMetrics);
document.getElementById('refreshBtn').addEventListener('click', loadMetrics);
