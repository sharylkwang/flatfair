const priceBox = document.querySelector('[data-price-check]');

if (priceBox) {
  const priceForm = priceBox.closest('form');

  const names = ['postalCode', 'block', 'town', 'flatType', 'flatModel', 'floorAreaSqm', 'storey', 'leaseCommenceYear', 'askingPrice'];

  const needed = {flatType: 'flat type', floorAreaSqm: 'size', storey: 'floor', leaseCommenceYear: 'lease year'};

  let timer = 0;

  let lastUrl = '';

  let revision = 0;

  function getValue(name) {
    const field = priceForm.elements[name];


    return field ? field.value.trim() : '';
  }

  function gaps() {
    const missing = Object.keys(needed).filter(name => !getValue(name)).map(name => needed[name]);

    if (!getValue('town') && !getValue('postalCode')) missing.push('postal code or town');


    return missing;
  }

  function wipeCost(message) {
    priceBox.textContent = message;

    lastUrl = '';
  }

  async function loadCost() {
    const missing = gaps();

    if (missing.length) {
      wipeCost('Fill in ' + missing.join(', ') + ' to see the price range.');



      return;
    }

    const query = new URLSearchParams();


    names.forEach(name => {
      if (getValue(name)) query.set(name, getValue(name));
    });

    const url = priceBox.dataset.priceCheck + '?' + query;



    if (url === lastUrl) return;

    const current = revision;


    lastUrl = url;


    try {
      const response = await fetch(url, {credentials: 'same-origin'});

      if (!response.ok) throw Error('Price check failed');


      const reply = await response.json();


      if (!reply.html) throw Error('Price range missing');



      if (current === revision) priceBox.innerHTML = reply.html;
    } catch (error) {
      if (current === revision) wipeCost('Could not check the price. Try again.');
    }
  }

  function loadSoon(event) {
    if (names.includes(event.target.name)) {
      clearTimeout(timer);


      revision += 1;

      const missing = gaps();


      wipeCost(missing.length ? 'Fill in ' + missing.join(', ') + ' to see the price range.' : 'Checking the updated price range…');

      timer = setTimeout(loadCost, 500);
    }
  }

  priceForm.addEventListener('input', loadSoon);

  priceForm.addEventListener('change', loadSoon);

  loadCost();
}
