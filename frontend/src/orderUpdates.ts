import {ref,onMounted,onUnmounted} from 'vue'
export const orderConnection=ref<'connecting'|'live'|'offline'>('connecting')
type Listener=(id?:string)=>void
const listeners=new Set<Listener>()
let source:EventSource|undefined
function broadcast(id?:string){for(const listener of listeners)listener(id)}
function connect(){
 if(source||!listeners.size||document.hidden)return
 orderConnection.value='connecting';source=new EventSource('/api/order-updates')
 source.addEventListener('ready',()=>{orderConnection.value='live';broadcast()})
 source.addEventListener('order',e=>{orderConnection.value='live';broadcast(JSON.parse((e as MessageEvent).data).id)})
 source.addEventListener('revoked',()=>{source?.close();source=undefined;orderConnection.value='offline';window.dispatchEvent(new Event('session-revoked'))})
 source.onerror=()=>{orderConnection.value='offline'}
}
function visibility(){if(document.hidden){source?.close();source=undefined}else connect()}
export function useOrderUpdates(listener:Listener){
 onMounted(()=>{listeners.add(listener);if(listeners.size===1)document.addEventListener('visibilitychange',visibility);connect()})
 onUnmounted(()=>{listeners.delete(listener);if(!listeners.size){source?.close();source=undefined;document.removeEventListener('visibilitychange',visibility)}})
}
// Coalesce bursts without a polling timer; changes received during a request
// trigger one additional fetch, so the final state cannot be lost.
export function serialRefresh(load:()=>Promise<void>){
 let active=false,pending=false
 return async()=>{pending=true;if(active)return;active=true;try{while(pending){pending=false;await load()}}finally{active=false}}
}
