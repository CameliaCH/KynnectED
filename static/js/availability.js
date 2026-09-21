function initializeAvailabilityPicker(days) {
  const picker = document.getElementById('part4');
  // Keep the existing Sunday-first arrays and ['empty'] unavailable value.
  const names = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
  const order = [1, 2, 3, 4, 5, 6, 0];
  const standardTimes = Array.from({ length: 14 }, (_, index) => `${String(index + 8).padStart(2, '0')}:00`);
  let activeDay = 1;
  const dayButtons = new Map();
  const copyInputs = new Map();
  const slots = picker.querySelector('.availability-slots');
  const week = document.getElementById('availability-week');
  const unavailable = picker.querySelector('.availability-unavailable');
  const customTime = document.getElementById('availability-start');
  const timeError = document.getElementById('availability-time-error');
  const copyButton = document.getElementById('availability-copy-button');
  const status = document.getElementById('availability-status');

  function timeRange(time) {
    const [hour, minute] = time.split(':').map(Number);
    const end = `${String((hour + 1) % 24).padStart(2, '0')}:${String(minute).padStart(2, '0')}`;
    return `${time}–${end}${hour === 23 ? ' (next day)' : ''}`;
  }

  function button(text, onClick, className = '') {
    const element = document.createElement('button');
    element.type = 'button';
    element.textContent = text;
    element.className = className;
    element.addEventListener('click', onClick);
    return element;
  }

  function selectDay(index) {
    activeDay = index;
    customTime.value = '';
    timeError.hidden = true;
    copyInputs.forEach(input => { input.checked = false; });
    render();
  }

  function updateDay(index, values, message) {
    days[index] = [...new Set(values)].sort();
    render();
    status.textContent = message;
    // Clear the wizard's missing-availability error once all days are answered.
    if (days.every(day => day.length > 0)) {
      document.getElementById('error').classList.remove('show');
    }
  }

  function toggleTime(time) {
    const selected = days[activeDay].includes(time);
    const values = days[activeDay].filter(value => value !== 'empty' && value !== time);
    if (!selected) values.push(time);
    updateDay(activeDay, values, `${names[activeDay]}: ${timeRange(time)} ${selected ? 'removed' : 'selected'}.`);
    // Rendering replaces slot buttons; restore keyboard focus to the same slot.
    slots.querySelector(`[data-time="${time}"]`)?.focus();
  }

  order.forEach(index => {
    const dayButton = button(names[index], () => selectDay(index), 'availability-day');
    dayButtons.set(index, dayButton);
    picker.querySelector('.availability-days').append(dayButton);

    const label = document.createElement('label');
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.addEventListener('change', updateCopyButton);
    label.append(input, document.createTextNode(names[index]));
    copyInputs.set(index, input);
    picker.querySelector('.availability-copy-days').append(label);
  });

  function updateCopyButton() {
    copyButton.disabled = days[activeDay].length === 0 ||
      ![...copyInputs.values()].some(input => !input.disabled && input.checked);
  }

  function render() {
    dayButtons.forEach((element, index) => {
      element.setAttribute('aria-pressed', String(index === activeDay));
      element.textContent = names[index] + (days[index].length ? ' ✓' : '');
      element.setAttribute('aria-label', `${names[index]}${days[index].length ? ', availability set' : ', not set'}`);
    });
    document.getElementById('availability-day-heading').textContent = `${names[activeDay]}'s availability`;
    unavailable.setAttribute('aria-pressed', String(days[activeDay].includes('empty')));
    slots.replaceChildren();
    const times = [...new Set([...standardTimes, ...days[activeDay].filter(time => time !== 'empty')])].sort();
    times.forEach(time => {
      const slot = button(timeRange(time), () => toggleTime(time), 'availability-slot');
      slot.dataset.time = time;
      slot.setAttribute('aria-pressed', String(days[activeDay].includes(time)));
      slots.append(slot);
    });

    week.replaceChildren();
    order.forEach(index => {
      const row = document.createElement('div');
      row.className = 'availability-summary-day';
      row.append(button(names[index], () => {
        selectDay(index);
        dayButtons.get(index).focus();
      }, 'availability-edit-day'));
      const selections = document.createElement('div');
      selections.className = 'availability-selections';
      if (days[index].length === 0 || days[index].includes('empty')) {
        const text = document.createElement('span');
        text.className = 'availability-help';
        text.textContent = days[index].length ? 'Unavailable' : 'Not set yet';
        selections.append(text);
      } else {
        days[index].forEach(time => {
          const chip = button(`${timeRange(time)} ×`, () => {
            updateDay(index, days[index].filter(value => value !== time), `${names[index]}: ${timeRange(time)} removed.`);
            week.querySelectorAll('.availability-edit-day')[order.indexOf(index)].focus();
          }, 'availability-chip');
          chip.setAttribute('aria-label', `Remove ${timeRange(time)} on ${names[index]}`);
          selections.append(chip);
        });
      }
      row.append(selections);
      week.append(row);
    });
    document.getElementById('availability-progress').textContent = `${days.filter(day => day.length > 0).length} of 7 days set. Select times or mark unavailable for each day.`;
    copyInputs.forEach((input, index) => {
      input.disabled = index === activeDay;
      input.closest('label').hidden = index === activeDay;
    });
    updateCopyButton();
  }

  unavailable.addEventListener('click', () => {
    const wasUnavailable = days[activeDay].includes('empty');
    updateDay(activeDay, wasUnavailable ? [] : ['empty'], `${names[activeDay]}: ${wasUnavailable ? 'availability cleared' : 'marked unavailable'}.`);
  });

  function addCustomTime() {
    const time = customTime.value;
    if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(time)) {
      timeError.hidden = false;
      customTime.focus();
      return;
    }
    timeError.hidden = true;
    updateDay(activeDay, [...days[activeDay].filter(value => value !== 'empty'), time], `${names[activeDay]}: ${timeRange(time)} selected.`);
    customTime.value = '';
    customTime.focus();
  }

  document.getElementById('availability-add').addEventListener('click', addCustomTime);
  customTime.addEventListener('keydown', event => {
    if (event.key === 'Enter') {
      event.preventDefault();
      addCustomTime();
    }
  });
  copyButton.addEventListener('click', () => {
    if (days[activeDay].length === 0) return;
    const targets = [];
    copyInputs.forEach((input, index) => {
      if (input.checked && !input.disabled) {
        days[index] = [...days[activeDay]];
        targets.push(names[index]);
        input.checked = false;
      }
    });
    updateDay(activeDay, days[activeDay], `Copied ${names[activeDay]}'s availability to ${targets.join(', ')}.`);
  });
  render();
}
