import {test} from 'node:test'
import assert from 'node:assert/strict'
import {minorAmount,currencyDigits} from '../src/currency.ts'

test('subscription currencies retain their actual minor units',()=>{
 assert.equal(minorAmount(12345,'PHP'),'123.45')
 assert.equal(minorAmount(12345,'JPY'),'12,345')
 assert.equal(minorAmount(12345,'XPF'),'12,345')
 assert.equal(minorAmount(12345,'OMR'),'12.345')
 assert.equal(minorAmount(12345,'BHD'),'12.345')
 assert.equal(currencyDigits('KWD'),3)
 assert.equal(minorAmount(12345,'ZZZ'),null)
 assert.equal(minorAmount(-1,'USD'),null)
})
