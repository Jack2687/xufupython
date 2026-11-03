export type ParsedCard={number:string;month:string;year:string;cvc:string}
export type CardLineResult={card:ParsedCard|null;error:string}

// Parse only in component memory. Do not include the original input in errors.
export function parseCardLine(raw:string,now=new Date()):CardLineResult {
  const invalid=(error:string):CardLineResult=>({card:null,error})
  if(!raw.trim())return invalid('')
  const parts=raw.trim().split(/\s*(?:-{2,}|[|｜;；,，\t\r\n]+)\s*/)
  let number='',month='',year='',cvc=''
  if(parts.length===3){
    const expiry=/^(\d{1,2})\s*\/\s*(\d{2}|\d{4})$/.exec(parts[1]||'')
    if(expiry){number=parts[0]||'';month=expiry[1]||'';year=expiry[2]||'';cvc=parts[2]||''}
  }else if(parts.length===4){[number='',month='',year='',cvc='']=parts}
  if(!number)return invalid('请按「卡号----MM/YY----CVV」或「卡号|MM|YYYY|CVV」粘贴。')
  number=number.replace(/[ -]/g,'')
  if(!/^\d{12,19}$/.test(number)||/^0+$/.test(number))return invalid('卡号格式不正确，请检查。')
  let sum=0,doubled=false
  for(let i=number.length-1;i>=0;i--){let digit=Number(number[i]);if(doubled){digit*=2;if(digit>9)digit-=9}sum+=digit;doubled=!doubled}
  if(sum%10!==0)return invalid('卡号校验未通过，请检查是否漏填或多填数字。')
  if(!/^\d{1,2}$/.test(month)||Number(month)<1||Number(month)>12||! /^(\d{2}|\d{4})$/.test(year))return invalid('有效期格式不正确，请使用 MM/YY 或 MM/YYYY。')
  if(year.length===2)year='20'+year
  if(Number(year)<now.getFullYear()||(Number(year)===now.getFullYear()&&Number(month)<now.getMonth()+1)||Number(year)>now.getFullYear()+25)return invalid('银行卡有效期已过或不正确，请检查。')
  if(!/^\d{3,4}$/.test(cvc))return invalid('安全码应为 3 或 4 位数字。')
  return {card:{number,month:month.padStart(2,'0'),year,cvc},error:''}
}
