export interface PriceRow {
  country:string; name:string; plan:string; currency:string; amount:number; amount_cny:number|null;
  website_amount:number; website_amount_cny:number|null; tax:string; website_tax:string;
  rate_to_cny:number|null; updated_at:string|null; stale:boolean; configured_currency:string; per_seat:boolean;
}
export interface PriceData { rows:PriceRow[]; fx_updated_at:string|null; fx_stale:boolean; scheduled:boolean; price_interval_hours:number }
export const emptyPrices=():PriceData=>({rows:[],fx_updated_at:null,fx_stale:false,scheduled:true,price_interval_hours:1})
export const pricePlans=[{id:'plus',name:'Plus'},{id:'pro5x',name:'Pro 5x'},{id:'pro20x',name:'Pro 20x'},{id:'pro50x',name:'Pro 50x'},{id:'team',name:'Team'}]
export const referencePlan=(id:string)=>({pro:'pro20x',promax:'pro50x',team_premium:'team'} as Record<string,string>)[id]||id
export const priceNumber=(v:number)=>v.toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2})
export const priceTime=(v:string|null)=>v?new Date(v).toLocaleString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'}):'尚未更新'
export const priceDate=(v:string|null)=>v?new Date(v).toLocaleDateString('zh-CN',{timeZone:'Asia/Shanghai'}):'—'
export const taxLabel=(tax:string)=>tax==='exclusive'?'税前价':tax==='inclusive'?'含税价':'税费口径待确认'
export const websiteLabel=(tax:string)=>tax==='inclusive'?'官网含税参考':tax==='exclusive'?'官网税前参考':'官网展示价'
export function countryQuote(data:PriceData,country:string,product:string,normalSeats=2,premiumSeats=0):PriceRow|undefined {
  const plan=referencePlan(product)
  const row=data.rows.find(r=>r.country===country&&r.plan===plan)
  if(plan!=='team')return row
  if(!Number.isInteger(normalSeats)||!Number.isInteger(premiumSeats)||normalSeats<0||premiumSeats<0||normalSeats+premiumSeats<2)return
  const premium=data.rows.find(r=>r.country===country&&r.plan==='team_premium')
  if((normalSeats&&!row)||(premiumSeats&&!premium))return
  const base=normalSeats?row:premium
  if(!base)return
  if(row&&premium&&normalSeats&&premiumSeats&&(row.currency!==premium.currency||row.tax!==premium.tax))return
  const amount=(row?.amount||0)*normalSeats+(premium?.amount||0)*premiumSeats
  const website=(row?.website_amount||0)*normalSeats+(premium?.website_amount||0)*premiumSeats
  return {...base,amount,website_amount:website,amount_cny:base.rate_to_cny?amount*base.rate_to_cny:null,
    website_amount_cny:base.rate_to_cny?website*base.rate_to_cny:null,
    stale:!!((normalSeats&&row?.stale)||(premiumSeats&&premium?.stale)),per_seat:false}
}
export function rankCountries<T extends {code:string;currency:string}>(countries:T[],data:PriceData,product:string,normal=2,premium=0){
  return countries.map((r,index)=>{
    const candidate=countryQuote(data,r.code,product,normal,premium)
    // A configured billing currency differing from the source cannot be quoted for this order.
    const quote=candidate?.currency===r.currency?candidate:undefined
    return {...r,price:quote,priceMissingReason:candidate&&!quote?'该结算币种暂无报价':'暂无参考价',originalIndex:index}
  }).sort((a,b)=>(a.price?.amount_cny??Infinity)-(b.price?.amount_cny??Infinity)||a.originalIndex-b.originalIndex)
}

