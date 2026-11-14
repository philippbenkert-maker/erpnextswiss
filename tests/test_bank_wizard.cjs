const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('erpnextswiss/erpnextswiss/page/bank_wizard/bank_wizard.js', 'utf8');
function setup() {
  const nodes = {}, events = {}, calls = [], messages = [], readers = [];
  for (const id of ['bank_account','input_file','table_placeholder','auto_process_matches','waitingScreen',
    'btn-parse-file','company','payable_account','receivable_account','expense_payable_account']) {
    nodes[id] = {value:'', files:[], innerHTML:'', disabled:false, classList:{add(){},remove(){}}, appendChild(){}};
  }
  nodes.bank_account.value='BANK-A';
  const frappe={pages:{bank_wizard:{}},render_template(){return '';},call(options){calls.push(options);},
    msgprint(message){messages.push(message);},show_alert(){}};
  class FileReader { constructor(){readers.push(this);} readAsDataURL(file){this.file=file;} }
  const page={main:{find(selector){return {on(event,fn){events[selector+':'+event]=fn;}};}}};
  const context={frappe,FileReader,console,document:{getElementById:id=>nodes[id],createElement:()=>({})},
    $:()=>({appendTo(){return this;}})};
  vm.createContext(context); vm.runInContext(source,context); frappe.bank_wizard.make(page);
  return {api:frappe.bank_wizard,nodes,events,calls,messages,readers,
    click:()=>events['.btn-parse-file:click']()};
}
test('missing file or explicit account produces no upload or stale actions',()=>{
  const s=setup();s.nodes.table_placeholder.innerHTML='old actions';s.click();
  assert.equal(s.calls.length,0);assert.equal(s.messages.length,1);assert.equal(s.nodes.table_placeholder.innerHTML,'');
  s.nodes.input_file.files=[{name:'bank.xml',size:5}];s.nodes.bank_account.value='';s.click();
  assert.equal(s.readers.length,0);assert.equal(s.calls.length,0);
});
test('oversized and unsupported files never reach the reader',()=>{
  const s=setup();for(const file of [{name:'bank.zip',size:33554433},{name:'bank.pdf',size:20},{name:'bank.xml',size:0}]){
    s.nodes.input_file.files=[file];s.click();
  }assert.equal(s.readers.length,0);assert.equal(s.calls.length,0);
});
test('original bytes use one POST even for rapid double click and release after failure',()=>{
  const s=setup();const bytes=Buffer.from([255,254,60,0,68,0,62,0]);
  s.nodes.input_file.files=[{name:'bank.XML',size:bytes.length}];s.click();s.click();
  assert.equal(s.readers.length,1);assert.equal(s.nodes.bank_account.disabled,true);
  s.readers[0].onload({target:{result:'data:application/xml;base64,'+bytes.toString('base64')}});
  assert.equal(s.calls.length,1);assert.equal(s.calls[0].type,'POST');
  assert.match(s.calls[0].method,/read_camt_file$/);
  assert.equal(s.calls[0].args.account,'BANK-A');
  assert.deepEqual(Buffer.from(s.calls[0].args.content_base64,'base64'),bytes);
  s.calls[0].error();s.calls[0].always();
  assert.equal(s.nodes['btn-parse-file'].disabled,false);assert.equal(s.api.busy,false);
  assert.equal(s.nodes.table_placeholder.innerHTML,'');
});
test('file read error enables retry and clears old results',()=>{
  const s=setup();s.nodes.input_file.files=[{name:'bank.zip',size:10}];s.click();s.readers[0].onerror();
  assert.equal(s.api.busy,false);assert.equal(s.calls.length,0);assert.equal(s.nodes.input_file.disabled,false);
});
test('failed account defaults cannot render a response that could trigger automatic booking',()=>{
  const s=setup();let renders=0;s.api.render_response=()=>renders++;
  s.api.parse('YQ==','BANK-A');s.calls[0].callback({message:{bank:'BANK-A',transactions:[],html:'actions'}});
  assert.equal(s.calls.length,2);assert.equal(s.calls[1].async,false);
  assert.equal(renders,0);assert.equal(s.nodes.auto_process_matches.value,0);
});
test('changing account or file invalidates previous actions',()=>{
  const s=setup();s.api.run();s.calls[0].callback({message:{accounts:['BANK-B']}});
  s.nodes.table_placeholder.innerHTML='BANK-A actions';s.nodes.bank_account.value='BANK-B';s.nodes.bank_account.onchange();
  assert.equal(s.nodes.table_placeholder.innerHTML,'');
  s.nodes.table_placeholder.innerHTML='BANK-B actions';s.events['#input_file:change']();
  assert.equal(s.nodes.table_placeholder.innerHTML,'');
});
