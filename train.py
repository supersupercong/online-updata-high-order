import os
import sys
import cv2
import argparse
import math

import torch
from torch import nn
from torch.nn import MSELoss
from torch.optim import Adam
from torch.optim.lr_scheduler import MultiStepLR
from torch.autograd import Variable
from torch.utils.data import DataLoader
from tensorboardX import SummaryWriter

import settings
from dataset import TrainValDataset, TestDataset
from model import ODE_DerainNet
from cal_ssim import SSIM

logger = settings.logger
os.environ['CUDA_VISIBLE_DEVICES'] = settings.device_id
torch.cuda.manual_seed_all(66)
torch.manual_seed(66)
import numpy as np


def ensure_dir(dir_path):
    if not os.path.isdir(dir_path):
        os.makedirs(dir_path)

def PSNR(img1, img2):
    b,_,_,_=img1.shape
    img1=np.clip(img1,0,255)
    img2=np.clip(img2,0,255)
    mse = np.mean((img1/ 255. - img2/ 255.) ** 2)
    if mse == 0:
        return 100

    PIXEL_MAX = 1
    return 20 * math.log10(PIXEL_MAX / math.sqrt(mse))        

class Session:
    def __init__(self):
        self.log_dir = settings.log_dir
        self.model_dir = settings.model_dir
        self.ssim_loss = settings.ssim_loss
        ensure_dir(settings.log_dir)
        ensure_dir(settings.model_dir)
        ensure_dir('../log_test')
        logger.info('set log dir as %s' % settings.log_dir)
        logger.info('set model dir as %s' % settings.model_dir)
        if len(settings.device_id) >1:
            self.net = nn.DataParallel(ODE_DerainNet()).cuda()
        else:
            torch.cuda.set_device(settings.device_id[0])
            self.net = ODE_DerainNet().cuda()
        self.l1 = nn.L1Loss().cuda()
        self.celoss = nn.CrossEntropyLoss().cuda()
        self.ssim = SSIM().cuda()
        self.step = 0
        self.save_steps = settings.save_steps
        self.num_workers = settings.num_workers
        self.batch_size = settings.batch_size
        self.writers = {}
        self.dataloaders = {}
        self.opt_net = Adam(self.net.parameters(), lr=settings.lr)
        self.sche_net = MultiStepLR(self.opt_net, milestones=[settings.l1, settings.l2], gamma=0.1)
       
    def tensorboard(self, name):
        self.writers[name] = SummaryWriter(os.path.join(self.log_dir, name + '.events'))
        return self.writers[name]

    def write(self, name, out):
        for k, v in out.items():
            self.writers[name].add_scalar(k, v, self.step)
        out['lr'] = self.opt_net.param_groups[0]['lr']
        out['step'] = self.step
        outputs = [
            "{}:{:.4g}".format(k, v) 
            for k, v in out.items()
        ]
        logger.info(name + '--' + ' '.join(outputs))

    def get_dataloader(self, dataset_name):
        dataset = TrainValDataset(dataset_name)
        if not dataset_name in self.dataloaders:
            self.dataloaders[dataset_name] = \
                    DataLoader(dataset, batch_size=self.batch_size, 
                            shuffle=True, num_workers=self.num_workers, drop_last=True)
        return iter(self.dataloaders[dataset_name])

    def get_test_dataloader(self, dataset_name):
        dataset = TestDataset(dataset_name)
        if not dataset_name in self.dataloaders:
            self.dataloaders[dataset_name] = \
                    DataLoader(dataset, batch_size=1, 
                            shuffle=False, num_workers=1, drop_last=False)
        return self.dataloaders[dataset_name]

    def save_checkpoints_net(self, name):
        ckp_path = os.path.join(self.model_dir, name)
        obj = {
            'net': self.net.state_dict(),
            'clock_net': self.step,
            'opt_net': self.opt_net.state_dict(),
        }
        torch.save(obj, ckp_path)

    def load_checkpoints_net(self, name):
        ckp_path = os.path.join(self.model_dir, name)
        try:
            logger.info('Load checkpoint %s' % ckp_path)
            obj = torch.load(ckp_path)
        except FileNotFoundError:
            logger.info('No checkpoint %s!!' % ckp_path)
            return
        self.net.load_state_dict(obj['net'])
        self.opt_net.load_state_dict(obj['opt_net'])
        self.step = obj['clock_net']
        self.sche_net.last_epoch = self.step

    def print_network(self, model):
        num_params = 0
        for p in model.parameters():
            num_params += p.numel()  
        print(model)
        print("The number of parameters: {}".format(num_params))

    def inf_batch(self, name, batch):
        if name == 'train':
            self.net.zero_grad()
        if self.step==0:
            self.print_network(self.net)

        # sample = {'O': O, 'B': B, 'O_2': O_2, 'O_4': O_4, 'O_8': O_8, 'B_2': B_2, 'B_4': B_4, 'B_8': B_8}

        O, B = batch['O'].cuda(), batch['B'].cuda()
        O_2, B_2 = batch['O_2'].cuda(), batch['B_2'].cuda()
        O_4, B_4 = batch['O_4'].cuda(), batch['B_4'].cuda()
        O_8, B_8 = batch['O_8'].cuda(), batch['B_8'].cuda()
        O, B = Variable(O, requires_grad=False), Variable(B, requires_grad=False)
        O_2, B_2 = Variable(O_2, requires_grad=False), Variable(B_2, requires_grad=False)
        O_4, B_4 = Variable(O_4, requires_grad=False), Variable(B_4, requires_grad=False)
        O_8, B_8 = Variable(O_8, requires_grad=False), Variable(B_8, requires_grad=False)
        out1, out2, out3, multiexit2, multiexit4 = self.net(O, O_2, O_4)

        ssim1 = self.ssim(out1, B)
        ssim2 = self.ssim(out2, B)
        ssim3 = self.ssim(out3, B)

        ssimmulti2 = self.ssim(multiexit2, B_2)
        ssimmulti4 = self.ssim(multiexit4, B_4)

        loss = -ssim1 - ssim2 - ssim3 - 0.05*ssimmulti2 - 0.001*ssimmulti4
        if name == 'train':
            loss.backward()
            self.opt_net.step()
        losses = {'L1loss2': loss}
        ssimes = {'ssim1': ssim1, 'ssim2': ssim2, 'ssim3': ssim3}
        losses.update(ssimes)
        self.write(name, losses)

        return out1

    def save_image(self, name, img_lists):
        data, pred, label = img_lists
        pred = pred.cpu().data

        data, label, pred = data * 255, label * 255, pred * 255
        pred = np.clip(pred, 0, 255)
        h, w = pred.shape[-2:]
        gen_num = (1, 1)
        img = np.zeros((gen_num[0] * h, gen_num[1] * 3 * w, 3))
        for img_list in img_lists:
            for i in range(gen_num[0]):
                row = i * h
                for j in range(gen_num[1]):
                    idx = i * gen_num[1] + j
                    tmp_list = [data[idx], pred[idx], label[idx]]
                    for k in range(3):
                        col = (j * 3 + k) * w
                        tmp = np.transpose(tmp_list[k], (1, 2, 0))
                        img[row: row+h, col: col+w] = tmp 
        img_file = os.path.join(self.log_dir, '%d_%s.jpg' % (self.step, name))
        cv2.imwrite(img_file, img)

    def inf_batch_test(self, name, batch):
        O, B = batch['O'].cuda(), batch['B'].cuda()
        O_2, B_2 = batch['O_2'].cuda(), batch['B_2'].cuda()
        O_4, B_4 = batch['O_4'].cuda(), batch['B_4'].cuda()
        O_8, B_8 = batch['O_8'].cuda(), batch['B_8'].cuda()
        O, B = Variable(O, requires_grad=False), Variable(B, requires_grad=False)
        O_2, B_2 = Variable(O_2, requires_grad=False), Variable(B_2, requires_grad=False)
        O_4, B_4 = Variable(O_4, requires_grad=False), Variable(B_4, requires_grad=False)
        O_8, B_8 = Variable(O_8, requires_grad=False), Variable(B_8, requires_grad=False)

        with torch.no_grad():
            out1, out2, out3, multiexit2, multiexit4 = self.net(O, O_2, O_4)

        l1_loss = self.l1(out1, B)
        ssim1 = self.ssim(out1, B)
        ssim2 = self.ssim(out2, B)
        ssim4 = self.ssim(out3, B)
        psnr1 = PSNR(out1.data.cpu().numpy() * 255, B.data.cpu().numpy() * 255)
        psnr2 = PSNR(out2.data.cpu().numpy() * 255, B.data.cpu().numpy() * 255)
        psnr4 = PSNR(out3.data.cpu().numpy() * 255, B.data.cpu().numpy() * 255)
        losses = {'L1 loss': l1_loss}
        ssimes = {'ssim1': ssim1,'ssim2': ssim2,'ssim4': ssim4}
        losses.update(ssimes)

        return l1_loss.data.cpu().numpy(), ssim1.data.cpu().numpy(), psnr1, ssim2.data.cpu().numpy(), psnr2, ssim4.data.cpu().numpy(), psnr4


def run_train_val(ckp_name_net='latest_net'):
    sess = Session()
    sess.load_checkpoints_net(ckp_name_net)
    sess.tensorboard('train')
    dt_train = sess.get_dataloader('train')
    while sess.step < settings.total_step+1:
        sess.sche_net.step()
        sess.net.train()
        try:
            batch_t = next(dt_train)
        except StopIteration:
            dt_train = sess.get_dataloader('train')
            batch_t = next(dt_train)
        pred_t = sess.inf_batch('train', batch_t)
        if sess.step % int(sess.save_steps / 16) == 0:
            sess.save_checkpoints_net('latest_net')
        if sess.step % sess.save_steps == 0:
            sess.save_image('train', [batch_t['O'], pred_t, batch_t['B']])

        #observe tendency of ssim, psnr and loss
        ssim1_all = 0
        psnr1_all = 0
        ssim2_all = 0
        psnr2_all = 0
        ssim4_all = 0
        psnr4_all = 0
        loss_all = 0
        num_all = 0
        if sess.step % (settings.one_epoch * 20) == 0:
            dt_val = sess.get_test_dataloader('test')
            sess.net.eval()
            for i, batch_v in enumerate(dt_val):
                loss, ssim1, psnr1,ssim2, psnr2,ssim4, psnr4= sess.inf_batch_test('test', batch_v)
                print(i)
                ssim1_all = ssim1_all + ssim1
                psnr1_all = psnr1_all + psnr1
                ssim2_all = ssim2_all + ssim2
                psnr2_all = psnr2_all + psnr2
                ssim4_all = ssim4_all + ssim4
                psnr4_all = psnr4_all + psnr4
                loss_all = loss_all + loss
                num_all = num_all + 1
            print('num_all:',num_all)
            loss_avg = loss_all / num_all
            ssim1_avg = ssim1_all / num_all
            psnr1_avg = psnr1_all / num_all
            ssim2_avg = ssim2_all / num_all
            psnr2_avg = psnr2_all / num_all
            ssim4_avg = ssim4_all / num_all
            psnr4_avg = psnr4_all / num_all
            logfile = open('../log_test/' + 'val' + '.txt','a+')
            epoch = int(sess.step / settings.one_epoch)
            logfile.write(
                'step  = ' + str(sess.step) + '\t'
                'epoch = ' + str(epoch) + '\t'
                'loss  = ' + str(loss_avg) + '\t'
                'ssim1  = ' + str(ssim1_avg) + '\t'
                'pnsr1  = ' + str(psnr1_avg) + '\t'
                'ssim2  = ' + str(ssim2_avg) + '\t'
                'pnsr2  = ' + str(psnr2_avg) + '\t'
                'ssim4  = ' + str(ssim4_avg) + '\t'
                'pnsr4  = ' + str(psnr4_avg) + '\t'
                '\n\n'
            )
            logfile.close()
        if sess.step % (settings.one_epoch*10) == 0:
            sess.save_checkpoints_net('net_%d_epoch' % int(sess.step / settings.one_epoch))
            logger.info('save model as net_%d_epoch' % int(sess.step / settings.one_epoch))
        sess.step += 1



if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-m1', '--model_1', default='latest_net')

    args = parser.parse_args(sys.argv[1:])
    run_train_val(args.model_1)

