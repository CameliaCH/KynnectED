(() => {
  const profiles = JSON.parse(document.getElementById('pairing-profiles').textContent);
  const days = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'];
  const title = value => String(value).replaceAll('-', ' ').replace(/\b\w/g, letter => letter.toUpperCase());

  for (const role of ['mentor', 'mentee']) {
    const select = document.getElementById(`${role}-id`);
    const container = document.getElementById(`${role}-profile`);
    select.addEventListener('change', render);
    function render() {
      container.replaceChildren();
      const profile = profiles.find(person => String(person.id) === select.value);
      if (!profile) {
        container.textContent = `Select a ${role} to see their profile.`;
        return;
      }
      const details = document.createElement('dl');
      const row = (label, value) => {
        const term = document.createElement('dt');
        term.textContent = label;
        const description = document.createElement('dd');
        description.textContent = value;
        details.append(term, description);
      };
      row('Subjects', (profile.subjects || []).map(title).join(', ') || 'Not provided');
      row('Age', profile.age ?? 'Not provided');
      row('Gender', title(profile.gender || 'Not provided'));
      row('Gender preference', (profile.gender_preferred || []).map(title).join(', ') || 'Not provided');
      row('Age preference', (profile.age_preferred || []).map(title).join(', ') || 'Not provided');
      row('Linked account IDs', (profile.partners || []).join(', ') || 'No pairings yet');
      container.append(details);
      const heading = document.createElement('h3');
      heading.textContent = 'Available session start times';
      container.append(heading);
      const list = document.createElement('ul');
      for (const day of days) {
        const times = profile.availability?.[day] || [];
        const item = document.createElement('li');
        item.textContent = `${title(day)}: ${times.includes('empty') ? 'Unavailable' : times.join(', ') || 'Not set'}`;
        list.append(item);
      }
      container.append(list);
    }
    render();
  }
})();
