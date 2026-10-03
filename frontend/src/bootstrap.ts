const configScript = document.createElement('script');
configScript.src = '/runtime-config.js';
configScript.onload = mountApplication;
configScript.onerror = mountApplication;
document.head.append(configScript);

function mountApplication() {
  void import('./main');
}
