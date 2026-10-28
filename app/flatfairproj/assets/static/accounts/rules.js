const rulesInput = document.getElementById(document.currentScript.dataset.for);


const ruleItems = document.querySelectorAll('.password-rules [data-rule]');


function checkRules() {


  for (const item of ruleItems) {
    const pattern = item.dataset.rule;



    let passed = false;



    if (new RegExp(pattern).test(rulesInput.value)) {



      passed = true;



    }



    item.classList.toggle('is-met', passed);


  }


}



rulesInput.addEventListener('input', checkRules);


checkRules();
