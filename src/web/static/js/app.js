/**
 * ML量化交易系统 - 前端工具函数
 */

// 格式化数字（千分位）
function formatNumber(num) {
    if (num === null || num === undefined || isNaN(num)) {
        return '0.00';
    }
    return parseFloat(num).toLocaleString('en-US', {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2
    });
}

// 格式化价格（自适应小数位）
function formatPrice(price) {
    if (price === null || price === undefined || isNaN(price)) {
        return '0';
    }
    price = parseFloat(price);
    if (price >= 1000) {
        return price.toFixed(2);
    } else if (price >= 1) {
        return price.toFixed(4);
    } else if (price >= 0.01) {
        return price.toFixed(6);
    } else {
        return price.toFixed(8);
    }
}

// 格式化百分比
function formatPercent(pct) {
    if (pct === null || pct === undefined || isNaN(pct)) {
        return '0.00%';
    }
    return parseFloat(pct).toFixed(2) + '%';
}

// 格式化日期时间
function formatDateTime(dateStr) {
    if (!dateStr) return '-';
    const date = new Date(dateStr);
    return date.toLocaleString('zh-CN', {
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit'
    });
}

// 格式化时间（仅时分）
function formatTime(dateStr) {
    if (!dateStr) return '-';
    const date = new Date(dateStr);
    return date.toLocaleTimeString('zh-CN', {
        hour: '2-digit',
        minute: '2-digit'
    });
}

// 格式化持续时间
function formatDuration(seconds) {
    if (!seconds || seconds < 0) return '-';

    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const secs = Math.floor(seconds % 60);

    if (hours > 0) {
        return hours + '时' + minutes + '分';
    } else if (minutes > 0) {
        return minutes + '分' + secs + '秒';
    } else {
        return secs + '秒';
    }
}

// 获取盈亏CSS类
function getPnlClass(pnl) {
    if (pnl > 0) return 'text-success';
    if (pnl < 0) return 'text-danger';
    return 'text-muted';
}

// 显示消息提示
function showToast(message, type) {
    type = type || 'info';
    const toastContainer = document.getElementById('toast-container');
    if (!toastContainer) {
        const container = document.createElement('div');
        container.id = 'toast-container';
        container.className = 'position-fixed bottom-0 end-0 p-3';
        container.style.zIndex = '11';
        document.body.appendChild(container);
    }

    const toast = document.createElement('div');
    toast.className = 'toast align-items-center text-white bg-' + type + ' border-0';
    toast.setAttribute('role', 'alert');

    const toastBody = document.createElement('div');
    toastBody.className = 'd-flex';

    const toastContent = document.createElement('div');
    toastContent.className = 'toast-body';
    toastContent.textContent = message;

    const closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'btn-close btn-close-white me-2 m-auto';
    closeBtn.setAttribute('data-bs-dismiss', 'toast');

    toastBody.appendChild(toastContent);
    toastBody.appendChild(closeBtn);
    toast.appendChild(toastBody);

    document.getElementById('toast-container').appendChild(toast);

    const bsToast = new bootstrap.Toast(toast, { delay: 3000 });
    bsToast.show();

    toast.addEventListener('hidden.bs.toast', function() {
        toast.remove();
    });
}

// API请求封装
async function apiRequest(url, options) {
    options = options || {};
    try {
        const response = await fetch(url, options);
        const data = await response.json();

        if (!data.success) {
            throw new Error(data.error || '请求失败');
        }

        return data.data;
    } catch (error) {
        console.error('API请求失败:', error);
        showToast(error.message, 'danger');
        throw error;
    }
}

// 防抖函数
function debounce(func, wait) {
    let timeout;
    return function executedFunction() {
        const context = this;
        const args = arguments;
        clearTimeout(timeout);
        timeout = setTimeout(function() {
            func.apply(context, args);
        }, wait);
    };
}

// 节流函数
function throttle(func, limit) {
    let inThrottle;
    return function() {
        const context = this;
        const args = arguments;
        if (!inThrottle) {
            func.apply(context, args);
            inThrottle = true;
            setTimeout(function() {
                inThrottle = false;
            }, limit);
        }
    };
}

// 复制到剪贴板
function copyToClipboard(text) {
    navigator.clipboard.writeText(text).then(function() {
        showToast('已复制到剪贴板', 'success');
    }).catch(function(err) {
        console.error('复制失败:', err);
        showToast('复制失败', 'danger');
    });
}

// 导出数据为CSV
function exportToCSV(data, filename) {
    if (!data || data.length === 0) {
        showToast('没有数据可导出', 'warning');
        return;
    }

    const headers = Object.keys(data[0]);
    const csvContent = [
        headers.join(','),
        ...data.map(function(row) {
            return headers.map(function(h) {
                let value = row[h];
                if (value === null || value === undefined) value = '';
                if (typeof value === 'string' && value.includes(',')) {
                    value = '"' + value + '"';
                }
                return value;
            }).join(',');
        })
    ].join('\n');

    const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = filename || 'export.csv';
    link.click();
}

// 页面加载完成后初始化
document.addEventListener('DOMContentLoaded', function() {
    // 添加loading状态管理
    const originalFetch = window.fetch;
    let activeRequests = 0;

    window.fetch = function() {
        activeRequests++;
        updateLoadingState();

        return originalFetch.apply(this, arguments)
            .then(function(response) {
                activeRequests--;
                updateLoadingState();
                return response;
            })
            .catch(function(error) {
                activeRequests--;
                updateLoadingState();
                throw error;
            });
    };

    function updateLoadingState() {
        // 可以在这里更新全局loading状态
    }
});
