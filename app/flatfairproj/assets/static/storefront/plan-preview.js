const planBox = document.querySelector('[data-plan-preview]');

if (planBox) {
  const planForm = planBox.closest('form');

  const planImage = planBox.querySelector('img');

  const planCaption = planBox.querySelector('[data-plan-caption]');

  const needed = ['flatType', 'floorAreaSqm', 'bedrooms', 'bathrooms'];

  const extra = ['flatModel', 'leaseCommenceYear', 'hasBalcony', 'block', 'postalCode', 'storey'];

  let planTimer = 0;

  let planUrl = '';

  function planText(name) {
    const field = planForm.elements[name];

    return field ? field.value.trim() : '';
  }

  function loadPlan() {
    if (needed.some(name => !planText(name))) {
      planBox.hidden = true;



      planUrl = '';


      return;
    }



    const query = new URLSearchParams();


    needed.concat(extra).forEach(name => {
      if (planText(name)) {
        query.set(name, planText(name));
      }
    });


    const url = planBox.dataset.planPreview + '?' + query;



    if (url === planUrl) {
      return;
    }


    planUrl = url;

    const kind = planForm.elements.flatType.selectedOptions[0].text;



    planImage.src = url;

    const size = kind + ', ' + planText('floorAreaSqm') + ' sqm';



    planImage.alt = 'Typical floor plan: ' + size;



    planCaption.textContent = 'Typical layout: ' + size + '. Rooms in your flat may differ.';


    planBox.hidden = false;
  }

  function planSoon(event) {
    if (needed.includes(event.target.name) || extra.includes(event.target.name)) {
      clearTimeout(planTimer);



      planTimer = setTimeout(loadPlan, 400);
    }
  }

  planImage.addEventListener('error', () => {
    if (planUrl && planImage.getAttribute('src') === planUrl) {
      planBox.hidden = true;
    }
  });

  planForm.addEventListener('input', planSoon);

  planForm.addEventListener('change', planSoon);

  loadPlan();
}
