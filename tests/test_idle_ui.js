const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
let list=[],timers=new Map(),serial=0,requests=[],active=0,maxActive=0,listeners={};
const element=()=>({value:'',hidden:false,disabled:false,append(){},replaceChildren(){},pause(){},getContext(){return {clearRect(){}}}});
const elements=new Map();
const document={hidden:false,getElementById(id){if(!elements.has(id))elements.set(id,element());return elements.get(id)},createElement:element,addEventListener(name,fn){listeners[name]=fn}};
const context=vm.createContext({document,Image:class{},console,confirm:()=>false,queueMicrotask,
 setTimeout(fn){const id=++serial;timers.set(id,fn);return id},clearTimeout(id){timers.delete(id)},
 async fetch(url){requests.push(url);if(url==='/api/jobs'){active++;maxActive=Math.max(maxActive,active);await new Promise(r=>setImmediate(r));active--;}
 return {ok:true,json:async()=>url==='/api/jobs'?list:url.startsWith('/api/files')?[]:{default:'/exports',description:'test'}};}});
vm.runInContext(fs.readFileSync('app/web/app.js','utf8'),context);
const settle=async()=>{for(let i=0;i<12;i++)await new Promise(r=>setImmediate(r));};
(async()=>{
 await settle();assert.equal(timers.size,0);assert.equal(requests.filter(x=>x==='/api/jobs').length,1);
 vm.runInContext('poll();poll();poll()',context);await settle();assert.equal(maxActive,1);assert.equal(timers.size,0);
 list=[{id:'x',path:'demo',state:'running',message:'test',progress:10}];vm.runInContext('poll()',context);await settle();assert.equal(timers.size,1);
 document.hidden=true;listeners.visibilitychange();assert.equal(timers.size,0);
 const count=requests.length;vm.runInContext('poll()',context);await settle();assert.equal(requests.length,count);
 list=[];document.hidden=false;listeners.visibilitychange();await settle();assert.equal(timers.size,0);
 console.log('Idle UI: no idle timers, no hidden polling, requests serialized, resumes once.');
})().catch(e=>{console.error(e);process.exitCode=1});
