"""Operator-only provisioning. Uses the normal AWS credential chain, never app keys.

No command changes the active SES ruleset or public Cloudflare routing implicitly.
"""
import argparse
import datetime as dt
import json
import uuid
from pathlib import Path
import boto3
from botocore.exceptions import ClientError

NAMES = ('one', 'two', 'three', 'four', 'five')


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def outputs(session, stack):
    return {v['OutputKey']: v['OutputValue'] for v in session.client('cloudformation').describe_stacks(StackName=stack)['Stacks'][0]['Outputs']}


def initialise(table, owner_sub):
    for name in NAMES:
        started=now(); generation=uuid.uuid4().hex
        try:
            table.put_item(Item={'pk':name,'sk':'META','state':'Reserved','generation':generation,
                                 'generation_started':started},ConditionExpression='attribute_not_exists(pk)')
        except ClientError as exc:
            if exc.response['Error']['Code']!='ConditionalCheckFailedException':raise
    table.put_item(Item={'pk':'ADMIN','sk':owner_sub,'enabled':True})


def assign(session, table, pool, name, login_email):
    current=table.get_item(Key={'pk':name,'sk':'META'},ConsistentRead=True)['Item']
    if current['state']!='Reserved':raise ValueError('Only a reserved mailbox can be assigned; disable/archive the previous assignment first')
    cognito=session.client('cognito-idp')
    try:
        user=cognito.admin_get_user(UserPoolId=pool,Username=login_email)
        attributes=user['UserAttributes']
    except cognito.exceptions.UserNotFoundException:
        # Cognito delivers the temporary invitation; never print or choose passwords.
        user=cognito.admin_create_user(UserPoolId=pool,Username=login_email,
            UserAttributes=[{'Name':'email','Value':login_email}],DesiredDeliveryMediums=['EMAIL'])['User']
        attributes=user['Attributes']
    sub=next(a['Value'] for a in attributes if a['Name']=='sub')
    generation=uuid.uuid4().hex; started=now()
    table.put_item(Item={'pk':name,'sk':'GEN#'+current.get('generation_started','0000'),
                         'generation':current['generation']})
    table.update_item(Key={'pk':name,'sk':'META'},
        UpdateExpression='SET #s=:s, subject=:u, generation=:g, generation_started=:t',
        ConditionExpression='#s=:reserved AND generation=:old',ExpressionAttributeNames={'#s':'state'},
        ExpressionAttributeValues={':s':'Assigned',':u':sub,':g':generation,':t':started,':reserved':'Reserved',':old':current['generation']})
    print(json.dumps({'mailbox':name,'state':'Assigned','login_email':login_email,
                      'claude_invitation':'Not sent. Owner must verify available seats and invite this employee as Member/Premium.'}))


def main():
    p=argparse.ArgumentParser();p.add_argument('--profile');p.add_argument('--stack',default='playloudr-mail');p.add_argument('--region',default='eu-west-1',choices=['eu-west-1'])
    sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('preflight')
    init=sub.add_parser('reserve');init.add_argument('--owner-sub',required=True)
    employee=sub.add_parser('assign');employee.add_argument('mailbox',choices=NAMES);employee.add_argument('--login-email',required=True)
    disable=sub.add_parser('disable');disable.add_argument('mailbox',choices=NAMES)
    release=sub.add_parser('reserve-again');release.add_argument('mailbox',choices=NAMES)
    sub.add_parser('connection')
    args=p.parse_args();session=boto3.Session(profile_name=args.profile,region_name=args.region)
    if args.command=='preflight':
        account=session.client('sesv2').get_account();active=session.client('ses').describe_active_receipt_rule_set()
        print(json.dumps({'region':args.region,'sending_enabled':account.get('SendingEnabled'),
            'production_access':account.get('ProductionAccessEnabled'),
            'active_receipt_rule_set':active.get('Metadata',{}).get('Name'),
            'existing_rules':[r['Name'] for r in active.get('Rules',[])]},indent=2));return
    out=outputs(session,args.stack);table=session.resource('dynamodb').Table(out['Table'])
    if args.command=='reserve':initialise(table,args.owner_sub)
    elif args.command=='assign':assign(session,table,out['UserPool'],args.mailbox,args.login_email)
    elif args.command=='disable':
        table.update_item(Key={'pk':args.mailbox,'sk':'META'},UpdateExpression='SET #s=:s',
            ConditionExpression='attribute_exists(pk)',ExpressionAttributeNames={'#s':'state'},ExpressionAttributeValues={':s':'Disabled'})
    elif args.command=='reserve-again':
        current=table.get_item(Key={'pk':args.mailbox,'sk':'META'},ConsistentRead=True)['Item']
        if current['state']!='Disabled':raise ValueError('Disable the previous employee first')
        table.put_item(Item={'pk':args.mailbox,'sk':'GEN#'+current.get('generation_started','0000'),'generation':current['generation']})
        table.update_item(Key={'pk':args.mailbox,'sk':'META'},
            UpdateExpression='SET #s=:s, generation=:g, generation_started=:t REMOVE subject',
            ConditionExpression='#s=:disabled AND generation=:old',ExpressionAttributeNames={'#s':'state'},
            ExpressionAttributeValues={':s':'Reserved',':g':uuid.uuid4().hex,':t':now(),':disabled':'Disabled',':old':current['generation']})
    elif args.command=='connection':
        print(json.dumps({'api_url':out['ApiUrl'],'auth_url':out['AuthUrl'],'client_id':out['ClientId'],'web_inbox':out['WebInbox']},indent=2))


if __name__=='__main__':main()
