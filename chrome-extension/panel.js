'use strict';
const fallback='http://127.0.0.1:8765';
function validate(value){const u=new URL(value);if((u.protocol==='https:'&&u.hostname.endsWith('.vercel.app'))||(u.protocol==='http:'&&['127.0.0.1','localhost'].includes(u.hostname)&&u.port==='8765'))return u.origin;throw Error('Use localhost:8765 or your HTTPS *.vercel.app deployment.')}
async function connect(url){const origin=validate(url);document.querySelector('#app-url').value=origin;document.querySelector('#app').src=origin;document.querySelector('#status').textContent=origin+' · same app, same collection';await chrome.storage.local.set({appUrl:origin})}
document.querySelector('#settings-toggle').onclick=()=>{document.querySelector('#settings').hidden=!document.querySelector('#settings').hidden};
document.querySelector('#settings').onsubmit=async e=>{e.preventDefault();try{await connect(document.querySelector('#app-url').value);document.querySelector('#settings').hidden=true}catch(error){document.querySelector('#status').textContent=error.message}};
chrome.storage.local.get('appUrl').then(s=>connect(s.appUrl||fallback)).catch(e=>document.querySelector('#status').textContent=e.message);
