const sizeData = document.getElementById('size-hints');

const sizeForm = document.querySelector('.sell-form');

if (sizeData && sizeForm) {
  const hints = JSON.parse(sizeData.textContent);

  const type = sizeForm.elements.flatType;



  const area = sizeForm.elements.floorAreaSqm;



  const note = document.createElement('span');

  note.setAttribute('aria-live', 'polite');



  area.closest('label').append(note);


  function sizeTip() {
    const rule = hints[type.value];


    const size = parseFloat(area.value);


    if (!rule) {
      note.textContent = '';


      return;
    }



    if (size && (size < rule.lo || size > rule.hi)) {
      note.className = 'field-warn';


      note.textContent = rule.label + ' flats are usually ' + rule.lo + ' to ' + rule.hi + ' sqm. Check your HDB papers or change the flat type.';
    } else {
      note.className = 'field-help';


      note.textContent = rule.label + ' flats are usually ' + rule.lo + ' to ' + rule.hi + ' sqm.';
    }
  }

  type.addEventListener('change', sizeTip);


  area.addEventListener('input', sizeTip);


  sizeTip();
}
