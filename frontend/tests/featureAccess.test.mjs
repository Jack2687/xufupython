import test from 'node:test'
import assert from 'node:assert/strict'
import {featureAllowed,routeAllowed,customerFeatures} from '../src/featureAccess.ts'
test('closed customer features cannot render by hash navigation',()=>{
 const flags=Object.fromEntries(customerFeatures.map(key=>[key,false]))
 for(const feature of customerFeatures)assert.equal(routeAllowed(flags,'/'+feature),false)
 assert.equal(routeAllowed(flags,'/profile'),true)
 assert.equal(routeAllowed(flags,'/admin/settings'),false)
 assert.equal(routeAllowed(flags,'/unknown'),false)
 flags.wallet=true
 assert.equal(routeAllowed(flags,'/wallet'),true)
 assert.equal(routeAllowed(flags,'/keys'),false)
})
test('existing sites retain customer features until configured',()=>{
 for(const feature of customerFeatures)assert.equal(featureAllowed(undefined,feature),true)
})
