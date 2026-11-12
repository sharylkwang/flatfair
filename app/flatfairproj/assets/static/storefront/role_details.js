(() => {
  var reveal = function (hash, scroll = false) {
    if (!hash || hash === '#') {
      return;
    }


    var id;



    try {
      id = decodeURIComponent(hash.slice(1));
    } catch (error) {
      return;
    }


    var target = document.getElementById(id);


    if (!target || !target.closest('.role-page, .admin-main, .calm-person-row')) {
      return;
    }



    for (var node = target; node; node = node.parentElement) {
      if (node.tagName === 'DETAILS') {
        node.open = true;
      }
    }



    if (scroll) {
      requestAnimationFrame(function () {
        target.scrollIntoView({ block: 'start' });
      });
    }
  };


  document.addEventListener('click', function (event) {
    var link = event.target.closest('a[href^="#"]');


    if (link) {
      reveal(link.hash);


    }
  });



  window.addEventListener('hashchange', function () {


    reveal(window.location.hash, true);
  });



  reveal(window.location.hash, true);
})();
