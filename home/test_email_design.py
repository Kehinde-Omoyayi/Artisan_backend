"""Read-only design tests; compatible with isolated template test settings."""
from django.core import mail
from django.template.loader import render_to_string
from django.test import SimpleTestCase, override_settings
from home.email_templates import send_security_code_email


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class WebsiteEmailDesignTests(SimpleTestCase):
    def security_context(self):
        return dict(subject='Verification code', recipient_name='Demo', heading='Complete your verification', intro='Enter this code to continue.', code='123456', code_label='VERIFICATION CODE', purpose_label='Account verification', security_note='Do not share your code.', preview_text='Your code is ready.')

    def test_security_purposes_share_the_website_shell(self):
        for purpose in ['Account verification','Password reset','Account security','Staff sign-in']:
            with self.subTest(purpose=purpose):
                context=self.security_context();context['purpose_label']=purpose
                html=render_to_string('emails/security_code.html',context)
                self.assertIn('123456',html)
                self.assertIn('single use',html)
                self.assertIn(purpose,html)
                self.assertIn('email-header',html)
                self.assertIn('email-footer',html)
                self.assertIn('#faf7f1',html)
                self.assertNotIn('<script',html)
                self.assertNotIn('<link',html)

    def test_notifications_with_and_without_actions(self):
        for action in ['', 'https://example.invalid/account/']:
            with self.subTest(action=action):
                html=render_to_string('emails/notification.html',dict(subject='Payment confirmed',recipient_name='Demo',message='Payment update.\nKeep your receipt.',action_url=action,action_label='View payment'))
                self.assertIn('Payment confirmed',html)
                self.assertIn('Payment update.<br>',html)
                self.assertIn('email-header',html)
                self.assertIn('email-footer',html)
                self.assertEqual('href=' in html,bool(action))
                if action:self.assertIn('View payment',html)

    def test_untrusted_names_and_messages_are_escaped(self):
        context=self.security_context();context['recipient_name']='<script>alert(1)</script>'
        html=render_to_string('emails/security_code.html',context)
        self.assertNotIn('<script>',html)
        self.assertIn('&lt;script&gt;',html)
        html=render_to_string('emails/notification.html',dict(subject='<img src=x>',recipient_name='Demo',message='<script>alert(1)</script>'))
        self.assertNotIn('<img src=x>',html)
        self.assertNotIn('<script>',html)
        self.assertIn('&lt;script&gt;',html)

    def test_security_delivery_keeps_plain_text_and_html(self):
        mail.outbox=[]
        result=send_security_code_email(recipient_email='demo@example.invalid',recipient_name='Demo',subject='Verification code',heading='Complete your verification',intro='Enter this code to continue.',code='123456',code_label='VERIFICATION CODE',security_note='Do not share your code.')
        self.assertEqual(result,1)
        self.assertEqual(len(mail.outbox),1)
        email=mail.outbox[0]
        self.assertIn('VERIFICATION CODE: 123456',email.body)
        self.assertNotIn('<html',email.body)
        self.assertEqual(email.alternatives[0].mimetype,'text/html')
        self.assertIn('email-header',email.alternatives[0].content)

    def test_staff_action_and_security_note_are_preserved(self):
        context=self.security_context();context.update(action_url='https://example.invalid/auth/staff-mfa/',action_label='Continue securely')
        html=render_to_string('emails/security_code.html',context)
        self.assertIn(context['action_url'],html)
        self.assertIn('Continue securely',html)
        self.assertIn('Do not share your code.',html)

    def test_real_mime_contains_related_brand_images_and_version_header(self):
        from home.email_brand import BrandedEmailMultiAlternatives
        from email import policy
        from email.parser import BytesParser
        email=BrandedEmailMultiAlternatives(subject='Demo',body='Plain text',to=['demo@example.invalid'])
        email.attach_alternative(render_to_string('emails/security_code.html',self.security_context()),'text/html')
        email.attach('receipt.pdf',b'%PDF-demo-fixture','application/pdf')
        parsed=BytesParser(policy=policy.default).parsebytes(email.message().as_bytes())
        self.assertEqual(parsed['X-Artisan-Email-Design'],'signin-v32')
        self.assertEqual(parsed.get_content_type(),'multipart/mixed')
        related=[p for p in parsed.walk() if p.get_content_type()=='multipart/related']
        self.assertEqual(len(related),1)
        self.assertEqual(related[0].get_payload()[0].get_content_type(),'text/html')
        cids={p.get('Content-ID') for p in parsed.walk() if p.get_content_type()=='image/png'}
        self.assertEqual(cids,{'<artisan-mark-v32>','<artisan-mark-light-v32>','<artisan-hero-v32>'})
        self.assertTrue(any(p.get_filename()=='receipt.pdf' and p.get_content_disposition()=='attachment' for p in parsed.walk()))

    def test_signin_layout_replaces_old_crafted_with_care_header(self):
        html=render_to_string('emails/security_code.html',self.security_context())
        self.assertIn('Good to have you back.',html)
        self.assertIn('cid:artisan-mark-v32',html)
        self.assertIn('cid:artisan-hero-v32',html)
        self.assertIn('signin-v32',html)
        self.assertNotIn('CRAFTED WITH CARE',html)
