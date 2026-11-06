import {test} from 'node:test'
import assert from 'node:assert/strict'
import {rankCountries,countryQuote,emptyPrices,referencePlan} from '../src/priceReference.ts'
const quote=(country,plan,amount,rate=1,currency='USD')=>({country,plan,currency,amount,amount_cny:rate==null?null:amount*rate,rate_to_cny:rate,website_amount:amount,website_amount_cny:rate==null?null:amount*rate,tax:'exclusive',website_tax:'exclusive',stale:false,name:country,configured_currency:currency,per_seat:false,updated_at:null})
test('countries sort by the selected plan, missing FX last, and do not mutate selection inputs',()=>{
 const countries=[{code:'US',currency:'USD'},{code:'PH',currency:'PHP'},{code:'JP',currency:'JPY'},{code:'SG',currency:'SGD'}]
 const data={...emptyPrices(),rows:[quote('US','plus',20,7),quote('PH','plus',980,.1,'PHP'),quote('JP','plus',2700,null,'JPY'),quote('US','pro20x',200,7),quote('PH','pro20x',9990,.2,'PHP')]}
 assert.deepEqual(rankCountries(countries,data,'plus').map(r=>r.code),['PH','US','JP','SG'])
 assert.deepEqual(rankCountries(countries,data,'pro').map(r=>r.code),['US','PH','JP','SG'])
 assert.deepEqual(countries.map(r=>r.code),['US','PH','JP','SG'])
 assert.equal(referencePlan('promax'),'pro50x')
})
test('Team combines standard and premium seats with consistent currency and tax basis',()=>{
 const data={...emptyPrices(),rows:[quote('US','team',25,7),quote('US','team_premium',100,7)]}
 assert.equal(countryQuote(data,'US','team',2,1).amount,150)
 assert.equal(countryQuote(data,'US','team',2,1).amount_cny,1050)
 assert.equal(countryQuote(data,'US','team',0,2).amount,200)
 assert.equal(countryQuote(data,'US','team',1,0),undefined)
 data.rows[1].currency='PHP'
 assert.equal(countryQuote(data,'US','team',2,1),undefined)
})
test('configured currency mismatches and unknown plans are never assigned a fabricated price',()=>{
 const data={...emptyPrices(),rows:[quote('PH','plus',980,.1,'PHP')]}
 assert.equal(rankCountries([{code:'PH',currency:'USD'}],data,'plus')[0].price,undefined)
 assert.equal(countryQuote(data,'PH','future-plan'),undefined)
 assert.equal(rankCountries([{code:'PH',currency:'PHP'}],data,'plus')[0].price.amount,980)
})

