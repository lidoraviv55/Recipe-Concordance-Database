// In-place "Show context" and "Update window" without full page reload
document.addEventListener('DOMContentLoaded', () => {
  const ctxContainer = document.getElementById('contexts-container');
  if (!ctxContainer) return;

  // Delegate clicks for "Show context" buttons (class js-show-context)
  document.body.addEventListener('click', async (e) => {
    const btn = e.target.closest('.js-show-context');
    if (!btn) return;

    e.preventDefault();
    const form = btn.closest('form');
    if (!form) return;

    // Build payload for AJAX endpoint
    const fd = new FormData();
    fd.append('sel_word', form.querySelector('[name="sel_word"]').value || '');
    fd.append('recipe_id', form.querySelector('[name="recipe_id"]').value || '');
    fd.append('group_id',  form.querySelector('[name="group_id"]').value  || '');
    fd.append('scope',     form.querySelector('[name="scope"]').value     || '');
    fd.append('ctx_window',form.querySelector('[name="ctx_window"]').value|| '5');

    const res = await fetch('/word-index/context', { method: 'POST', body: fd });
    const html = await res.text();
    ctxContainer.innerHTML = html;

    // Re-run highlighter if present
    highlightKwic();

    // Scroll into view
    ctxContainer.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });

  // Delegate submit for "Update window" inside the contexts block (class js-update-window)
  document.body.addEventListener('submit', async (e) => {
    const form = e.target.closest('.js-update-window');
    if (!form) return;

    e.preventDefault();

    // We need sel_word from the heading (or hidden input in the partial)
    const selWord = form.querySelector('[name="sel_word"]').value || '';
    const win = form.querySelector('[name="ctx_window"]').value || '5';

    // Also carry over the current filters from the main filter form if present
    const filterForm = document.getElementById('wi-filter-form');
    const rid  = filterForm ? (filterForm.querySelector('[name="recipe_id"]').value || '') : '';
    const gid  = filterForm ? (filterForm.querySelector('[name="group_id"]').value  || '') : '';
    const scp  = filterForm ? (filterForm.querySelector('[name="scope"]').value     || '') : '';

    const fd = new FormData();
    fd.append('sel_word', selWord);
    fd.append('ctx_window', win);
    fd.append('recipe_id', rid);
    fd.append('group_id',  gid);
    fd.append('scope',     scp);

    const res = await fetch('/word-index/context', { method: 'POST', body: fd });
    const html = await res.text();
    ctxContainer.innerHTML = html;

    // Re-run highlighter if present
    highlightKwic();

    // Keep scroll in place
    ctxContainer.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
});

function highlightKwic() {
  const list = document.getElementById('kwic-list');
  if (!list) return;
  const needle = list.getAttribute('data-needle');
  if (!needle) return;

  const escapeRx = s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const rx = new RegExp(`\\b(${escapeRx(needle)})\\b`, 'gi');

  list.querySelectorAll('.kwic-snippet').forEach(el => {
    el.innerHTML = el.textContent.replace(rx, '<span class="kwic-hit">$1</span>');
  });
}

// Apply semantic colors to badges based on content
document.addEventListener('DOMContentLoaded', () => {
  // Color badges based on text content
  document.querySelectorAll('.badge').forEach((badge, index) => {
    const text = badge.textContent.trim().toUpperCase();
    
    // Skip if already has specific color class
    if (badge.classList.contains('text-bg-secondary') || 
        badge.classList.contains('text-bg-light') ||
        badge.classList.contains('text-bg-primary') ||
        badge.classList.contains('text-bg-success') ||
        badge.classList.contains('text-bg-danger') ||
        badge.classList.contains('text-bg-warning')) {
      return;
    }
    
    // Apply colors based on content
    if (text === 'TITLE') {
      badge.style.background = '#4C8BF5';
      badge.style.color = '#ffffff';
    } else if (text === 'INGREDIENT') {
      badge.style.background = '#1DA462';
      badge.style.color = '#ffffff';
    } else if (text === 'STEP') {
      badge.style.background = '#FFCD46';
      badge.style.color = '#1a1a1a';
    } else {
      // Alternating colors for other badges
      const colors = ['#4C8BF5', '#1DA462', '#FFCD46', '#DD5144'];
      const colorIndex = index % 4;
      badge.style.background = colors[colorIndex];
      badge.style.color = colorIndex === 2 ? '#1a1a1a' : '#ffffff';
    }
  });
  
  // Add colored borders to cards based on position
  document.querySelectorAll('.card').forEach((card, index) => {
    const colors = ['#4C8BF5', '#1DA462', '#FFCD46', '#DD5144'];
    const colorIndex = index % 4;
    if (!card.querySelector('.card-header::before')) {
      const header = card.querySelector('.card-header');
      if (header) {
        header.style.borderLeft = `4px solid ${colors[colorIndex]}`;
        header.style.paddingLeft = 'calc(1.25rem - 4px)';
      }
    }
  });
  
  // Color table cells based on content
  document.querySelectorAll('.table tbody td').forEach((td, index) => {
    const colors = [
      { bg: 'rgba(76,139,245,0.06)', border: '#4C8BF5' },
      { bg: 'rgba(29,164,98,0.06)', border: '#1DA462' },
      { bg: 'rgba(255,205,70,0.06)', border: '#FFCD46' },
      { bg: 'rgba(221,81,68,0.06)', border: '#DD5144' }
    ];
    const colIndex = index % 4;
    if (!td.style.borderLeft || td.style.borderLeft === 'none') {
      td.style.borderLeft = `3px solid ${colors[colIndex].border}`;
      td.style.paddingLeft = 'calc(0.75rem + 3px)';
    }
  });
  
  // Color form inputs
  document.querySelectorAll('.form-control, .form-select').forEach((input, index) => {
    const colors = ['#4C8BF5', '#1DA462', '#FFCD46', '#DD5144'];
    const colorIndex = index % 4;
    if (!input.style.borderLeft || input.style.borderLeft.includes('transparent')) {
      input.style.borderLeft = `3px solid ${colors[colorIndex]}`;
      input.style.paddingLeft = 'calc(0.85rem + 3px)';
    }
  });
  
  // Color list items more vibrantly
  document.querySelectorAll('.list-group-item').forEach((item, index) => {
    const colors = [
      { bg: 'rgba(76,139,245,0.08)', border: '#4C8BF5' },
      { bg: 'rgba(29,164,98,0.08)', border: '#1DA462' },
      { bg: 'rgba(255,205,70,0.08)', border: '#FFCD46' },
      { bg: 'rgba(221,81,68,0.08)', border: '#DD5144' }
    ];
    const colorIndex = index % 4;
    item.style.borderLeft = `4px solid ${colors[colorIndex].border}`;
    item.style.background = colors[colorIndex].bg;
    item.style.paddingLeft = 'calc(1.25rem + 4px)';
  });
  
  // Color section headers
  document.querySelectorAll('h2, h3, h4, h5, h6').forEach((heading, index) => {
    const colors = ['#4C8BF5', '#1DA462', '#FFCD46', '#DD5144'];
    const colorIndex = index % 4;
    if (!heading.style.borderBottom) {
      heading.style.borderBottom = `3px solid ${colors[colorIndex]}`;
      heading.style.paddingBottom = '0.5rem';
      heading.style.marginBottom = '1rem';
    }
  });
  
  // Color table rows with alternating patterns
  document.querySelectorAll('.table tbody tr').forEach((row, index) => {
    const colors = [
      { bg: 'rgba(76,139,245,0.03)', hover: 'rgba(76,139,245,0.08)' },
      { bg: 'rgba(29,164,98,0.03)', hover: 'rgba(29,164,98,0.08)' },
      { bg: 'rgba(255,205,70,0.03)', hover: 'rgba(255,205,70,0.08)' },
      { bg: 'rgba(221,81,68,0.03)', hover: 'rgba(221,81,68,0.08)' }
    ];
    const colorIndex = index % 4;
    row.style.background = colors[colorIndex].bg;
    row.addEventListener('mouseenter', () => {
      row.style.background = colors[colorIndex].hover;
    });
    row.addEventListener('mouseleave', () => {
      row.style.background = colors[colorIndex].bg;
    });
  });
});