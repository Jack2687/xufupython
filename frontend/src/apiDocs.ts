type Row = Record<string, any>

export function resolveSchema(spec:Row, value:Row={}):Row {
  if(value.$ref)return {...spec.components?.schemas?.[value.$ref.split('/').at(-1)],...value}
  return value
}

export function schemaFields(spec:Row,value:Row={},prefix='',depth=0):Row[] {
  if(depth>5)return []
  const s=resolveSchema(spec,value)
  const object=s.properties?s:(s.anyOf||s.oneOf||[]).map((x:Row)=>resolveSchema(spec,x)).find((x:Row)=>x.properties)||s
  return Object.entries(object.properties||{}).flatMap(([key,raw])=>{
    const f=resolveSchema(spec,raw as Row),name=prefix+key
    const item=f.items?resolveSchema(spec,f.items):null
    return [{...f,name,required:object.required?.includes(key),nested:!!prefix},...schemaFields(spec,item||f,name+(item?'[].':'.'),depth+1)]
  })
}

export function fieldType(v:Row):string {
  if(v.anyOf||v.oneOf)return (v.anyOf||v.oneOf).map(fieldType).join(' / ')
  if(v.const!==undefined)return JSON.stringify(v.const)
  if(v.enum)return v.enum.join(' / ')
  if(v.$ref)return v.$ref.split('/').at(-1)
  if(v.type==='array')return fieldType(v.items||{})+'[]'
  return Array.isArray(v.type)?v.type.join(' / '):v.type||'object'
}

export function requestExamples(x:Row):Row {
  const content=x.op.requestBody?.content?.['application/json']
  return content?.examples||(content?.example?{default:{summary:'请求示例',value:content.example}}:{})
}

export function codeSample(x:Row,origin:string,lang:string,exampleKey?:string):string {
  const examples=requestExamples(x),body=examples[exampleKey||Object.keys(examples)[0]]?.value
  let url=origin+x.path
  for(const p of x.op.parameters||[])if(p.in==='path')url=url.replace('{'+p.name+'}',p.example||'RECORD_ID')
  const query=(x.op.parameters||[]).filter((p:Row)=>p.in==='query'&&p.required)
  if(query.length)url+='?'+query.map((p:Row)=>p.name+'='+encodeURIComponent(p.example||'VALUE')).join('&')
  const headers:Row={Authorization:'Bearer YOUR_API_KEY'}
  for(const p of x.op.parameters||[])if(p.in==='header'&&p.required)headers[p.name]=p.example||'VALUE'
  if(body!==undefined)headers['Content-Type']='application/json'
  if(lang==='python')return `import os\nimport json\nimport requests\n\nheaders = ${JSON.stringify(headers,null,2).replace('"Bearer YOUR_API_KEY"','"Bearer " + os.environ["XUFU_API_KEY"]')}\n${body!==undefined?'payload = json.loads('+JSON.stringify(JSON.stringify(body))+')\n':''}response = requests.${x.method}(\n    ${JSON.stringify(url)}, headers=headers, ${body!==undefined?'json=payload, ':''}timeout=30\n)\nresponse.raise_for_status()\nprint(response.json())`
  if(lang==='node')return `const response = await fetch(${JSON.stringify(url)}, {\n  method: ${JSON.stringify(x.method.toUpperCase())},\n  signal: AbortSignal.timeout(30000),\n  headers: ${JSON.stringify(headers,null,2).replace('"Bearer YOUR_API_KEY"','"Bearer " + process.env.XUFU_API_KEY')}${body!==undefined?',\n  body: JSON.stringify('+JSON.stringify(body,null,2)+')':''}\n});\nconst result = await response.json();\nif (!response.ok) throw new Error(result.error || response.statusText);\nconsole.log(result);`
  const quote=(v:string)=>"'"+v.replaceAll("'","'\\''")+"'"
  return `curl --fail-with-body --connect-timeout 10 --max-time 30 ${quote(url)} \\\n  -X ${x.method.toUpperCase()} \\\n  `+Object.entries(headers).map(([k,v])=>k==='Authorization'?'-H "Authorization: Bearer $XUFU_API_KEY"':`-H ${quote(k+': '+v)}`).join(' \\\n  ')+(body!==undefined?` \\\n  --data-raw ${quote(JSON.stringify(body,null,2))}`:'')
}
