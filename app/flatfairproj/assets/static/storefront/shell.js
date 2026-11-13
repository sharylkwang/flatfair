const frame = document.getElementById('app');

const key = 'flatfairPath';

const asked = frame.dataset.start || '/';

let start = asked;



const entry = performance.getEntriesByType('navigation')[0];



const returning = entry && (entry.type === 'reload' || entry.type === 'back_forward');



if (asked === '/' && returning) start = sessionStorage.getItem(key) || '/';



history.replaceState(null, '', '/');


frame.src = start === '/' ? '/?home=1' : start;


let first = true;


frame.addEventListener('load', () => {
  let inner;



  try {
    inner = frame.contentWindow.location;


    sessionStorage.setItem(key, inner.pathname + inner.search + inner.hash);



    document.title = frame.contentDocument.title || 'FlatFair';
  } catch (error) {
    return;
  }



  if (first) {
    first = false;


    frame.contentWindow.focus();
  }
});
